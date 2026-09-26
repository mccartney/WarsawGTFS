"""Compare two GTFS zip files table by table and print a Markdown report.

Usage: python tools/compare_gtfs.py OURS.zip THEIRS.zip [--examples N] [--fail-unexpected]

With --fail-unexpected, exits with code 1 if the feeds differ in anything else than EXPECTED:
the build timestamp and OSM-based shapes, which depend on when each feed was built.

Rows are matched by each table's primary key. Only one side of a table is kept in memory
at a time, so even stop_times.txt of warsaw.zip fits comfortably.
"""

import csv
import io
import sys
from argparse import ArgumentParser
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from zipfile import ZipFile

PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "agency.txt": ("agency_id",),
    "attributions.txt": ("attribution_id",),
    "calendar.txt": ("service_id",),
    "calendar_dates.txt": ("service_id", "date"),
    "fare_attributes.txt": ("fare_id",),
    "feed_info.txt": (),  # a single row
    "frequencies.txt": ("trip_id", "start_time"),
    "routes.txt": ("route_id",),
    "shapes.txt": ("shape_id", "shape_pt_sequence"),
    "stop_times.txt": ("trip_id", "stop_sequence"),
    "stops.txt": ("stop_id",),
    "trips.txt": ("trip_id",),
}

SEP = "\x1f"

# Differences which come from build time and OSM extract age, not from data or code.
# None means any difference in that file is expected.
EXPECTED: dict[str, set[str] | None] = {
    "feed_info.txt": {"feed_version", "feed_publisher_name", "feed_publisher_url"},
    "shapes.txt": None,
    "stop_times.txt": {"shape_dist_traveled"},
}


@dataclass
class TableDiff:
    name: str
    columns: list[str]
    rows_ours: int = 0
    rows_theirs: int = 0
    only_ours: list[str] = field(default_factory=list)
    only_theirs: list[str] = field(default_factory=list)
    changed: int = 0
    changed_columns: Counter[str] = field(default_factory=Counter)
    examples: list[tuple[str, str, str, str]] = field(default_factory=list)

    @property
    def identical(self) -> bool:
        return not (self.only_ours or self.only_theirs or self.changed)

    @property
    def expected(self) -> bool:
        if self.identical or (self.name in EXPECTED and EXPECTED[self.name] is None):
            return True
        allowed = EXPECTED.get(self.name) or set[str]()
        return not (self.only_ours or self.only_theirs) and set(self.changed_columns) <= allowed


def read_rows(arch: ZipFile, name: str) -> tuple[list[str], Iterator[dict[str, str]]]:
    f = io.TextIOWrapper(arch.open(name), encoding="utf-8-sig", newline="")
    reader = csv.DictReader(f)
    return list(reader.fieldnames or []), reader


def compare_table(ours: ZipFile, theirs: ZipFile, name: str, max_examples: int) -> TableDiff:
    cols_ours, rows_ours = read_rows(ours, name)
    cols_theirs, rows_theirs = read_rows(theirs, name)
    columns = cols_ours + [c for c in cols_theirs if c not in cols_ours]
    key_cols = PRIMARY_KEYS.get(name, tuple(columns))  # unknown table: the whole row is the key
    value_cols = [c for c in columns if c not in key_cols]
    diff = TableDiff(name, columns)

    def key(row: dict[str, str]) -> str:
        return SEP.join(row.get(c, "") for c in key_cols)

    def values(row: dict[str, str]) -> str:
        return SEP.join(row.get(c, "") for c in value_cols)

    theirs_by_key = dict[str, str]()
    for row in rows_theirs:
        theirs_by_key[key(row)] = values(row)
        diff.rows_theirs += 1

    for row in rows_ours:
        diff.rows_ours += 1
        k = key(row)
        their_values = theirs_by_key.pop(k, None)
        if their_values is None:
            diff.only_ours.append(k)
            continue
        our_values = values(row)
        if our_values == their_values:
            continue
        diff.changed += 1
        for col, a, b in zip(value_cols, our_values.split(SEP), their_values.split(SEP)):
            if a != b:
                diff.changed_columns[col] += 1
                if len(diff.examples) < max_examples:
                    diff.examples.append((k, col, a, b))

    diff.only_theirs = list(theirs_by_key)
    return diff


def fmt_key(k: str) -> str:
    return "`" + k.replace(SEP, " / ") + "`"


def summarize_keys(keys: list[str], limit: int) -> str:
    shown = ", ".join(fmt_key(k) for k in keys[:limit])
    return shown + (f", … (+{len(keys) - limit})" if len(keys) > limit else "")


def group_counts(keys: list[str], part: int = 0) -> Counter[str]:
    """Counts keys by their first component (e.g. trip_id of stop_times, shape_id of shapes)."""
    return Counter(k.split(SEP)[part] for k in keys)


def report(ours_path: str, theirs_path: str, max_examples: int) -> tuple[str, bool]:
    """Returns the Markdown report and whether all differences are expected."""
    out = list[str]()
    with ZipFile(ours_path) as ours, ZipFile(theirs_path) as theirs:
        names_ours = {n for n in ours.namelist() if n.endswith(".txt")}
        names_theirs = {n for n in theirs.namelist() if n.endswith(".txt")}

        out.append(f"# GTFS comparison\n\n- ours: `{ours_path}`\n- theirs: `{theirs_path}`\n")
        if names_ours != names_theirs:
            out.append(f"- files only in ours: {sorted(names_ours - names_theirs)}")
            out.append(f"- files only in theirs: {sorted(names_theirs - names_ours)}\n")

        diffs = [compare_table(ours, theirs, n, max_examples) for n in sorted(names_ours & names_theirs)]

    unexpected = [d.name for d in diffs if not d.expected]
    if names_ours != names_theirs:
        unexpected.append("(set of files)")
    if unexpected:
        out.append(f"**Unexpected differences in: {', '.join(unexpected)}**\n")
    else:
        out.append("Only expected differences (build time, OSM-based shapes).\n")

    out.append("| file | rows ours | rows theirs | only ours | only theirs | changed | changed columns |")
    out.append("|---|---:|---:|---:|---:|---:|---|")
    for d in diffs:
        cols = ", ".join(f"{c} ({n})" for c, n in d.changed_columns.most_common()) or ("—" if d.identical else "")
        out.append(
            f"| {d.name} | {d.rows_ours} | {d.rows_theirs} | {len(d.only_ours)} "
            f"| {len(d.only_theirs)} | {d.changed} | {cols} |"
        )

    for d in diffs:
        if d.identical:
            continue
        out.append(f"\n## {d.name}\n")
        if d.name in ("stop_times.txt", "shapes.txt") and (d.only_ours or d.only_theirs):
            what = "trips" if d.name == "stop_times.txt" else "shapes"
            out.append(f"- {what} with rows only in ours: {len(group_counts(d.only_ours))}")
            out.append(f"- {what} with rows only in theirs: {len(group_counts(d.only_theirs))}")
        if d.only_ours:
            out.append(f"- only in ours: {summarize_keys(d.only_ours, max_examples)}")
        if d.only_theirs:
            out.append(f"- only in theirs: {summarize_keys(d.only_theirs, max_examples)}")
        for k, col, a, b in d.examples:
            out.append(f"- {fmt_key(k)} `{col}`: ours `{a}` vs theirs `{b}`")

    return "\n".join(out) + "\n", not unexpected


def main() -> None:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ours")
    parser.add_argument("theirs")
    parser.add_argument("--examples", type=int, default=10, help="examples per table")
    parser.add_argument("--fail-unexpected", action="store_true", help="exit 1 on unexpected diffs")
    args = parser.parse_args()
    text, ok = report(args.ours, args.theirs, args.examples)
    sys.stdout.write(text)
    if args.fail_unexpected and not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
