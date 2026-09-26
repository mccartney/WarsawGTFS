"""Sanity-check a freshly built warsaw.zip before it is published.

Usage: python tools/check_gtfs.py NEW.zip [--previous OLD.zip] [--min-ratio 0.8] [--min-days 6]

Fails (exit code 1) if a required file is missing, if the calendar does not cover today plus
the following days, or if a table shrank noticeably compared to the previously published feed.
"""

import csv
import io
import sys
from argparse import ArgumentParser
from datetime import date, datetime, timedelta
from zipfile import ZipFile
from zoneinfo import ZoneInfo

REQUIRED = [
    "agency.txt",
    "calendar_dates.txt",
    "feed_info.txt",
    "routes.txt",
    "shapes.txt",
    "stop_times.txt",
    "stops.txt",
    "trips.txt",
]
COUNTED = ["routes.txt", "stops.txt", "trips.txt", "stop_times.txt", "shapes.txt"]
TZ = ZoneInfo("Europe/Warsaw")


def count_rows(arch: ZipFile, name: str) -> int:
    with arch.open(name) as f:
        return sum(1 for _ in f) - 1  # no quoted newlines in this feed


def service_dates(arch: ZipFile) -> set[date]:
    f = io.TextIOWrapper(arch.open("calendar_dates.txt"), encoding="utf-8-sig", newline="")
    return {
        datetime.strptime(r["date"], "%Y%m%d").date()
        for r in csv.DictReader(f)
        if r["exception_type"] == "1"
    }


def main() -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("new")
    parser.add_argument("--previous", help="previously published zip, if any")
    parser.add_argument("--min-ratio", type=float, default=0.8)
    parser.add_argument("--min-days", type=int, default=6, help="today + N-1 following days")
    args = parser.parse_args()
    errors = list[str]()

    with ZipFile(args.new) as new:
        missing = [n for n in REQUIRED if n not in new.namelist()]
        if missing:
            print(f"missing files: {missing}")
            return 1

        today = datetime.now(TZ).date()
        dates = service_dates(new)
        wanted = {today + timedelta(days=i) for i in range(args.min_days)}
        if not_covered := sorted(wanted - dates):
            errors.append(f"calendar_dates.txt has no service on {[str(d) for d in not_covered]}")
        print(f"service dates: {min(dates)} .. {max(dates)}")

        counts = {n: count_rows(new, n) for n in COUNTED}

    if args.previous:
        with ZipFile(args.previous) as old:
            for name, n in counts.items():
                o = count_rows(old, name) if name in old.namelist() else 0
                print(f"{name}: {n} rows (previous {o})")
                if o and n < args.min_ratio * o:
                    errors.append(f"{name} shrank from {o} to {n} rows")
    else:
        for name, n in counts.items():
            print(f"{name}: {n} rows")

    for e in errors:
        print(f"ERROR: {e}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
