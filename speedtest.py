#!/usr/bin/env python3
"""Измеряет скорость скачивания: N последовательных запросов к одному адресу.

Пример:
    python3 speedtest.py https://example.com/big.jpg
    python3 speedtest.py https://example.com/big.jpg -n 5 --timeout 10
"""

from __future__ import annotations

import argparse
import http.client
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, TextIO
from urllib.parse import urlparse

DEFAULT_COUNT = 10
DEFAULT_TIMEOUT = 30.0
CHUNK_SIZE = 64 * 1024

# identity — чтобы считать реально переданные байты без сжатия;
# no-cache — просим промежуточные кэши не отдавать сохранённую копию.
REQUEST_HEADERS = {
    "User-Agent": "speed-test/1.0",
    "Accept-Encoding": "identity",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

BYTES_IN_MB = 1_000_000  # десятичные мегабайты (10^6)
BITS_IN_MBIT = 1_000_000  # десятичные мегабиты (10^6)


@dataclass(frozen=True)
class Result:
    """Один успешный запрос: сколько длился и сколько байт тела получено."""

    seconds: float
    size: int


@dataclass(frozen=True)
class Summary:
    succeeded: int
    failed: int
    total_bytes: int
    total_seconds: float

    @property
    def avg_seconds(self) -> float:
        return self.total_seconds / self.succeeded if self.succeeded else 0.0

    @property
    def megabits_per_second(self) -> float:
        # Общий объём / общее время, а не среднее скоростей отдельных запросов.
        return speed_mbit(self.total_bytes, self.total_seconds)

    @property
    def megabytes_per_second(self) -> float:
        return self.megabits_per_second / 8


def speed_mbit(size: int, seconds: float) -> float:
    if seconds <= 0:
        return 0.0
    return size * 8 / seconds / BITS_IN_MBIT


def summarize(results: Iterable[Result], failed: int = 0) -> Summary:
    results = list(results)
    return Summary(
        succeeded=len(results),
        failed=failed,
        total_bytes=sum(r.size for r in results),
        total_seconds=sum(r.seconds for r in results),
    )


def fetch(url: str, timeout: float) -> Result:
    """Скачивает тело ответа целиком (кусками, не держа в памяти).

    Время — от отправки запроса до получения последнего байта тела,
    включая DNS, установку TCP/TLS-соединения и редиректы.
    """
    request = urllib.request.Request(url, headers=REQUEST_HEADERS)
    start = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        size = 0
        while True:
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                break
            size += len(chunk)
    return Result(seconds=time.perf_counter() - start, size=size)


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)
    return str(exc) or type(exc).__name__


def format_mb(size: int) -> str:
    return f"{size / BYTES_IN_MB:.2f} МБ"


def run(url: str, count: int, timeout: float, out: Optional[TextIO] = None) -> Summary:
    if out is None:
        out = sys.stdout
    print(f"Адрес: {url}", file=out)
    print(f"Запросов: {count}, таймаут: {timeout:g} с\n", file=out)

    width = len(str(count))
    results = []
    failed = 0
    for i in range(1, count + 1):
        prefix = f"[{i:>{width}}/{count}]"
        try:
            result = fetch(url, timeout)
        except urllib.error.HTTPError as exc:
            exc.close()
            failed += 1
            print(f"{prefix} ошибка: {describe_error(exc)}", file=out)
            continue
        except (OSError, http.client.HTTPException) as exc:
            failed += 1
            print(f"{prefix} ошибка: {describe_error(exc)}", file=out)
            continue
        results.append(result)
        print(
            f"{prefix} {result.seconds:8.3f} с  {format_mb(result.size):>12}  "
            f"{speed_mbit(result.size, result.seconds):9.2f} Мбит/с",
            file=out,
        )
        out.flush()

    summary = summarize(results, failed)
    print_summary(summary, out)
    return summary


def print_summary(summary: Summary, out: TextIO) -> None:
    total = summary.succeeded + summary.failed
    print("\nИтог", file=out)
    print(f"  Успешных запросов:     {summary.succeeded} из {total}", file=out)
    if not summary.succeeded:
        print("  Ни один запрос не завершился успешно — скорость не посчитать.", file=out)
        return
    print(f"  Среднее время запроса: {summary.avg_seconds:.3f} с", file=out)
    print(f"  Скачано всего:         {format_mb(summary.total_bytes)}", file=out)
    print(
        f"  Скорость скачивания:   {summary.megabits_per_second:.2f} Мбит/с "
        f"({summary.megabytes_per_second:.2f} МБ/с)",
        file=out,
    )


def http_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise argparse.ArgumentTypeError(
            f"нужен адрес вида http(s)://host/path, получено: {value!r}"
        )
    return value


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"нужно целое число, получено: {value!r}")
    if number < 1:
        raise argparse.ArgumentTypeError(f"нужно число не меньше 1, получено: {number}")
    return number


def positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"нужно число, получено: {value!r}")
    if not number > 0:
        raise argparse.ArgumentTypeError(f"нужно число больше 0, получено: {value!r}")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Делает несколько последовательных запросов к адресу, дожидаясь полного "
            "ответа на каждый, и печатает среднее время запроса, общий объём и "
            "скорость скачивания в Мбит/с."
        )
    )
    parser.add_argument("url", type=http_url, help="адрес, к которому делать запросы")
    parser.add_argument(
        "-n",
        "--count",
        type=positive_int,
        default=DEFAULT_COUNT,
        help=f"сколько запросов сделать (по умолчанию {DEFAULT_COUNT})",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=positive_float,
        default=DEFAULT_TIMEOUT,
        help=(
            "таймаут в секундах на подключение и на каждое чтение из сокета "
            f"(по умолчанию {DEFAULT_TIMEOUT:g})"
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = run(args.url, args.count, args.timeout)
    except KeyboardInterrupt:
        print("\nПрервано.", file=sys.stderr)
        return 130
    return 0 if summary.succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
