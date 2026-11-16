"""Тесты speedtest.py. Поднимают локальный HTTP-сервер, сеть не нужна."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import speedtest  # noqa: E402

PAYLOAD = os.urandom(200_000)


class Handler(BaseHTTPRequestHandler):
    server: "TestServer"

    def log_message(self, format, *args):  # тишина в выводе тестов
        pass

    def do_GET(self):
        srv = self.server
        with srv.lock:
            srv.hits += 1
            hit = srv.hits
            srv.arrivals.append(time.perf_counter())
            srv.headers.append(dict(self.headers))

        if self.path == "/file":
            self._send_payload()
        elif self.path == "/slow":
            self._send_payload(slow=True)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/file")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/flaky":
            if hit % 2:
                self.send_error(500, "Boom")
            else:
                self._send_payload()
        elif self.path == "/hang":
            time.sleep(1.0)
            self._send_payload()
        else:
            self.send_error(404, "Not Found")

    def _send_payload(self, slow: bool = False):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        if not slow:
            self.wfile.write(PAYLOAD)
            return
        half = len(PAYLOAD) // 2
        self.wfile.write(PAYLOAD[:half])
        self.wfile.flush()
        time.sleep(0.05)
        # Момент *до* отправки последней части: клиент физически не может
        # получить конец тела раньше, значит и следующий запрос — тоже.
        with self.server.lock:
            self.server.last_part_sent.append(time.perf_counter())
        self.wfile.write(PAYLOAD[half:])


class TestServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.hits = 0
        self.arrivals = []
        self.headers = []
        self.last_part_sent = []

    def handle_error(self, request, client_address):
        pass  # обрывы соединения от таймаут-тестов не интересны


class ServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = TestServer()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.server.reset()

    def run_script(self, path, count=3, timeout=5.0):
        out = io.StringIO()
        summary = speedtest.run(self.base + path, count, timeout, out=out)
        return summary, out.getvalue()


class SummaryTests(unittest.TestCase):
    def test_speed_is_total_bytes_over_total_time(self):
        results = [speedtest.Result(1.0, 1_000_000), speedtest.Result(3.0, 1_000_000)]
        s = speedtest.summarize(results)
        self.assertEqual(s.succeeded, 2)
        self.assertEqual(s.total_bytes, 2_000_000)
        self.assertAlmostEqual(s.total_seconds, 4.0)
        self.assertAlmostEqual(s.avg_seconds, 2.0)
        # 2 МБ * 8 / 4 с = 4 Мбит/с. Среднее скоростей дало бы (8 + 2.67) / 2 = 5.33.
        self.assertAlmostEqual(s.megabits_per_second, 4.0)
        self.assertAlmostEqual(s.megabytes_per_second, 0.5)

    def test_empty_results(self):
        s = speedtest.summarize([], failed=3)
        self.assertEqual((s.succeeded, s.failed, s.total_bytes), (0, 3, 0))
        self.assertEqual(s.avg_seconds, 0.0)
        self.assertEqual(s.megabits_per_second, 0.0)

    def test_speed_mbit_units(self):
        # 1 250 000 байт за 1 с = 10 000 000 бит/с = 10 Мбит/с (десятичные).
        self.assertAlmostEqual(speedtest.speed_mbit(1_250_000, 1.0), 10.0)
        self.assertEqual(speedtest.speed_mbit(100, 0), 0.0)


class FetchTests(ServerTestCase):
    def test_downloads_full_body_and_counts_bytes(self):
        summary, output = self.run_script("/file", count=3)
        self.assertEqual(self.server.hits, 3)
        self.assertEqual(summary.succeeded, 3)
        self.assertEqual(summary.failed, 0)
        self.assertEqual(summary.total_bytes, 3 * len(PAYLOAD))
        self.assertGreater(summary.total_seconds, 0)
        self.assertIn("Мбит/с", output)
        self.assertIn("Успешных запросов:     3 из 3", output)

    def test_requests_are_sequential_and_wait_for_full_response(self):
        self.run_script("/slow", count=4)
        arrivals = self.server.arrivals
        sent = self.server.last_part_sent
        self.assertEqual(len(arrivals), 4)
        for i in range(3):
            self.assertGreater(
                arrivals[i + 1],
                sent[i],
                f"запрос {i + 2} пришёл до того, как отправлен конец ответа {i + 1}",
            )

    def test_sends_no_compression_and_no_cache_headers(self):
        self.run_script("/file", count=1)
        headers = {k.lower(): v for k, v in self.server.headers[0].items()}
        self.assertEqual(headers["accept-encoding"], "identity")
        self.assertEqual(headers["cache-control"], "no-cache")

    def test_follows_redirects(self):
        summary, _ = self.run_script("/redirect", count=2)
        self.assertEqual(summary.succeeded, 2)
        self.assertEqual(summary.total_bytes, 2 * len(PAYLOAD))

    def test_http_errors_are_reported_and_skipped(self):
        summary, output = self.run_script("/missing", count=2)
        self.assertEqual(summary.succeeded, 0)
        self.assertEqual(summary.failed, 2)
        self.assertIn("HTTP 404", output)
        self.assertIn("скорость не посчитать", output)

    def test_partial_failures_counted_separately(self):
        summary, output = self.run_script("/flaky", count=4)
        self.assertEqual(summary.succeeded, 2)
        self.assertEqual(summary.failed, 2)
        self.assertEqual(summary.total_bytes, 2 * len(PAYLOAD))
        self.assertIn("HTTP 500", output)
        self.assertIn("Успешных запросов:     2 из 4", output)

    def test_timeout_is_a_failure(self):
        summary, output = self.run_script("/hang", count=1, timeout=0.2)
        self.assertEqual(summary.failed, 1)
        self.assertIn("ошибка", output)

    def test_connection_refused_is_a_failure(self):
        out = io.StringIO()
        # Порт 1 на localhost почти наверняка закрыт.
        summary = speedtest.run("http://127.0.0.1:1/", 1, 2.0, out=out)
        self.assertEqual(summary.failed, 1)


class CliTests(ServerTestCase):
    def call_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = speedtest.main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_default_count_is_10(self):
        code, _, _ = self.call_main(self.base + "/file")
        self.assertEqual(code, 0)
        self.assertEqual(self.server.hits, 10)

    def test_count_option(self):
        code, _, _ = self.call_main(self.base + "/file", "-n", "2")
        self.assertEqual(code, 0)
        self.assertEqual(self.server.hits, 2)

    def test_defaults(self):
        args = speedtest.build_parser().parse_args(["http://x"])
        self.assertEqual(args.count, 10)
        self.assertEqual(args.timeout, 30.0)

    def test_exit_code_1_when_all_failed(self):
        code, _, _ = self.call_main(self.base + "/missing", "-n", "1")
        self.assertEqual(code, 1)

    def test_invalid_arguments(self):
        for argv in (
            ["ftp://example.com/file"],
            ["example.com/file"],
            [self.base + "/file", "-n", "0"],
            [self.base + "/file", "-n", "abc"],
            [self.base + "/file", "--timeout", "0"],
            [self.base + "/file", "--timeout", "-1"],
        ):
            with self.subTest(argv=argv):
                code, _, err = self.call_main(*argv)
                self.assertEqual(code, 2)
                self.assertIn("error", err)
        self.assertEqual(self.server.hits, 0)

    def test_runs_as_script(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        proc = subprocess.run(
            [sys.executable, str(ROOT / "speedtest.py"), self.base + "/file", "-n", "2"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Скорость скачивания:", proc.stdout)
        self.assertEqual(self.server.hits, 2)


if __name__ == "__main__":
    unittest.main()
