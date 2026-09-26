"""Rewrite feed_info.txt of a GTFS zip with a different publisher, keeping everything else.

Usage: python tools/set_feed_publisher.py GTFS.zip --name NAME --url URL

Upstream's app.py hardcodes mkuran as the publisher; patching it here instead of there keeps
this fork's copy of app.py identical to upstream, so the daily upstream merge stays conflict-free.
"""

import csv
import io
import os
from argparse import ArgumentParser
from zipfile import ZIP_DEFLATED, ZipFile


def main() -> None:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("gtfs")
    parser.add_argument("--name", required=True)
    parser.add_argument("--url", required=True)
    args = parser.parse_args()

    tmp = args.gtfs + ".tmp"
    with ZipFile(args.gtfs) as src, ZipFile(tmp, "w", ZIP_DEFLATED) as dst:
        for info in src.infolist():
            if info.filename != "feed_info.txt":
                with src.open(info) as i, dst.open(info, "w") as o:
                    while chunk := i.read(1 << 20):
                        o.write(chunk)
                continue

            reader = csv.DictReader(io.TextIOWrapper(src.open(info), encoding="utf-8-sig"))
            rows = list(reader)
            for row in rows:
                row["feed_publisher_name"] = args.name
                row["feed_publisher_url"] = args.url
            buf = io.StringIO()
            writer = csv.DictWriter(buf, reader.fieldnames or [], lineterminator="\r\n")
            writer.writeheader()
            writer.writerows(rows)
            dst.writestr(info, buf.getvalue().encode("utf-8"))

    os.replace(tmp, args.gtfs)


if __name__ == "__main__":
    main()
