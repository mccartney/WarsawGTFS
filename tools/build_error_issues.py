"""Turn the ERROR lines of a build log into GitHub issues, one per problem.

Usage: python tools/build_error_issues.py BUILD.log [--gtfs warsaw.zip] [--run-url URL] [--dry-run]

Errors are grouped by stop group (the first 4 digits of a stop id), so both directions of a leg,
and a "too long" and a "no route" between the same two groups, share one issue. Each issue carries
a hidden key; for every key seen in the log:

* an open issue: nothing, except a comment when an error not seen on it before shows up;
* closed as completed in the last REOPEN_DAYS days: reopened with a comment;
* closed as completed earlier: a new issue, pointing to the old one;
* closed as not planned (or duplicate): ignored for good, or until the date given in an
  "ignore-until: YYYY-MM-DD" line in its body or a comment;
* none: a new issue.

Issues are never closed automatically. Needs GITHUB_TOKEN (issues: write) and GITHUB_REPOSITORY,
unless --dry-run, which only prints what it would do.
"""

import csv
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from argparse import ArgumentParser
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator
from zipfile import ZipFile

LABEL = "build-error"
REOPEN_DAYS = 7

ANSI = re.compile(r"\x1b\[[0-9;]*m")
# [ERROR 03:08:42.535] Final.Task.GenerateBusShapes: Shape between ...
LOG_LINE = re.compile(r"\[ERROR [\d:.]+\] ([^:]+): (.*)$")
KEY_MARKER = re.compile(r"<!-- build-error-key: (.+?) -->")
SIG_MARKER = re.compile(r"<!-- build-error-sig: (.+?) -->")
IGNORE_UNTIL = re.compile(r"^\s*ignore-until:\s*(\d{4}-\d{2}-\d{2})\s*$", re.M | re.I)

PAIR_ERRORS = [
    (re.compile(r"Shape between (\d+) and (\d+) is too long"), "Shape too long"),
    (re.compile(r"No route exists between stops (\d+) and (\d+)"), "No route"),
]
STOP_ERRORS = [
    (re.compile(r"Stop (\d+) is too far from nearest node"), "Stop too far from OSM node"),
]


@dataclass
class Error:
    logger: str
    message: str
    kind: str
    sig: str  # the message without the numbers that change from build to build
    stops: tuple[str, ...]  # (from, to) for a leg, (stop,) for a stop, () otherwise

    @property
    def task(self) -> str:
        return self.logger.rpartition(".")[2]


@dataclass
class Problem:
    key: str
    errors: list[Error] = field(default_factory=list[Error])


def stop_group(stop_id: str) -> str:
    return stop_id[:4] if len(stop_id) == 6 and stop_id.isdigit() else stop_id


def classify(logger: str, message: str) -> tuple[str, Error]:
    for pattern, kind in PAIR_ERRORS:
        if m := pattern.search(message):
            a, b = m.groups()
            groups = sorted((stop_group(a), stop_group(b)))
            return f"leg:{groups[0]}-{groups[1]}", Error(logger, message, kind, m[0], (a, b))
    for pattern, kind in STOP_ERRORS:
        if m := pattern.search(message):
            stop = m[1]
            return f"stop:{stop_group(stop)}", Error(logger, message, kind, m[0], (stop,))
    sig = re.sub(r"\d+(\.\d+)?", "#", message)
    return f"other:{logger}: {sig}", Error(logger, message, "Other", sig, ())


def parse_log(lines: Iterator[str]) -> dict[str, Problem]:
    problems: dict[str, Problem] = {}
    for line in lines:
        if not (m := LOG_LINE.search(ANSI.sub("", line).rstrip())):
            continue
        key, error = classify(m[1], m[2])
        problem = problems.setdefault(key, Problem(key))
        if all(e.sig != error.sig for e in problem.errors):
            problem.errors.append(error)
    return problems


# GTFS lookups


@dataclass
class Usage:
    """Trips of one route over one leg (or through one stop)."""

    trips: int = 0
    examples: dict[str, str] = field(default_factory=dict[str, str])  # variant_code -> trip_id


class Gtfs:
    def __init__(self, path: str, problems: dict[str, Problem]) -> None:
        self.arch = ZipFile(path)
        self.stops = {r["stop_id"]: r for r in self.read("stops.txt")}
        self.routes = {r["route_id"]: r["route_short_name"] for r in self.read("routes.txt")}
        self.trips = {r["trip_id"]: r for r in self.read("trips.txt")}

        legs = {e.stops for p in problems.values() for e in p.errors if len(e.stops) == 2}
        stops = {e.stops[0] for p in problems.values() for e in p.errors if len(e.stops) == 1}
        self.usage: dict[tuple[str, ...], dict[str, Usage]] = defaultdict(dict)
        self.scan_stop_times(legs, stops)

    def read(self, name: str) -> Iterator[dict[str, str]]:
        with self.arch.open(name) as f:
            yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))

    def scan_stop_times(self, legs: set[tuple[str, ...]], stops: set[str]) -> None:
        # stop_times.txt is grouped by trip and sorted by stop_sequence
        prev_trip, prev_stop = "", ""
        for r in self.read("stop_times.txt"):
            trip, stop = r["trip_id"], r["stop_id"]
            if trip == prev_trip and (prev_stop, stop) in legs:
                self.add_usage((prev_stop, stop), trip)
            if stop in stops:
                self.add_usage((stop,), trip)
            prev_trip, prev_stop = trip, stop

    def add_usage(self, where: tuple[str, ...], trip_id: str) -> None:
        trip = self.trips[trip_id]
        route = self.routes.get(trip["route_id"], trip["route_id"])
        u = self.usage[where].setdefault(route, Usage())
        u.trips += 1
        u.examples.setdefault(trip.get("variant_code") or "?", trip_id)

    def stop_name(self, stop_id: str) -> str:
        if not (s := self.stops.get(stop_id)):
            return "?"
        return f"{s['stop_name']} {s.get('stop_code', '')}".strip()

    def group_name(self, group: str) -> str:
        # Any stop of the group will do, they share the name apart from the code
        for stop_id, s in self.stops.items():
            if stop_group(stop_id) == group:
                return s["stop_name"]
        return group

    def stop_link(self, stop_id: str) -> str:
        if not (s := self.stops.get(stop_id)):
            return ""
        lat, lon = s["stop_lat"], s["stop_lon"]
        return f"[map](https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=18/{lat}/{lon})"


# Rendering


def problem_title(problem: Problem, gtfs: Gtfs | None) -> str:
    kind, _, rest = problem.key.partition(":")
    name = gtfs.group_name if gtfs else (lambda g: g)
    if kind == "leg":
        a, b = rest.split("-")
        kinds = "/".join(sorted({e.kind for e in problem.errors}))
        where = f"{name(a)} ↔ {name(b)}" if a != b else f"within {name(a)}"
        return f"{kinds}: {where} ({rest})"
    if kind == "stop":
        return f"{problem.errors[0].kind}: {name(rest)} ({rest})"
    return f"Build error: {problem.errors[0].message[:100]}"


def render_error(error: Error, gtfs: Gtfs | None) -> str:
    out = [f"#### {error.kind} ({error.task})", "", "```", error.message, "```", ""]
    if not error.stops:
        return "\n".join(out)
    if gtfs is None:
        out += [f"Stops: {', '.join(error.stops)} (no GTFS to look up the details)", ""]
        return "\n".join(out)

    roles = ["from", "to"] if len(error.stops) == 2 else ["stop"]
    out += ["| | Stop | Name | |", "|---|---|---|---|"]
    out += [
        f"| {role} | `{s}` | {gtfs.stop_name(s)} | {gtfs.stop_link(s)} |"
        for role, s in zip(roles, error.stops)
    ]
    out.append("")

    usage = gtfs.usage.get(error.stops, {})
    if not usage:
        out += ["No trips found using this in the GTFS.", ""]
        return "\n".join(out)
    out += ["| Line | Trips | Variant: example trip |", "|---|---|---|"]
    for line in sorted(usage, key=lambda r: (len(r), r)):
        u = usage[line]
        examples = "<br>".join(f"{v}: `{t}`" for v, t in sorted(u.examples.items()))
        out.append(f"| {line} | {u.trips} | {examples} |")
    out.append("")
    return "\n".join(out)


def render_errors(errors: list[Error], gtfs: Gtfs | None) -> str:
    sigs = "".join(f"<!-- build-error-sig: {e.sig} -->\n" for e in errors)
    return sigs + "\n".join(render_error(e, gtfs) for e in errors)


def render_body(problem: Problem, gtfs: Gtfs | None, run: str, previous: int | None) -> str:
    out = [f"<!-- build-error-key: {problem.key} -->", f"Raised from the build log of {run}."]
    if previous:
        out.append(f"The same problem was reported before in #{previous}.")
    out += [
        "",
        render_errors(problem.errors, gtfs),
        "---",
        "To ignore this for good, close it as *not planned*. To ignore it only for a while (e.g. a "
        "diversion), also add a line `ignore-until: YYYY-MM-DD` in a comment.",
    ]
    return "\n".join(out)


# GitHub


class GitHub:
    def __init__(self, repo: str, token: str | None) -> None:
        self.repo = repo
        self.token = token

    def request(self, method: str, path: str, data: Any = None) -> Any:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}/{path}",
            method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            },
        )
        with urllib.request.urlopen(req) as resp:
            return json.load(resp) if resp.status != 204 else None

    def paginate(self, path: str) -> Iterator[Any]:
        page = 1
        while items := self.request("GET", f"{path}&per_page=100&page={page}"):
            yield from items
            page += 1

    def issues(self) -> Iterator[Any]:
        for issue in self.paginate(f"issues?labels={LABEL}&state=all&sort=created&direction=desc"):
            if "pull_request" not in issue:
                yield issue

    def comments(self, number: int) -> list[str]:
        return [c["body"] or "" for c in self.paginate(f"issues/{number}/comments?")]

    def ensure_label(self) -> None:
        try:
            self.request("POST", "labels", {"name": LABEL, "color": "d93f0b"})
        except urllib.error.HTTPError as e:
            if e.code != 422:  # already exists
                raise


def ignored(texts: list[str], today: date) -> bool:
    dates = [date.fromisoformat(d) for t in texts for d in IGNORE_UNTIL.findall(t)]
    return not dates or max(dates) >= today


def sync(problems: dict[str, Problem], gh: GitHub, gtfs: Gtfs | None, run: str, dry: bool) -> None:
    def act(what: str, method: str, path: str, data: Any) -> None:
        print(what)
        if not dry:
            gh.request(method, path, data)

    by_key: dict[str, Any] = {}
    try:
        for issue in gh.issues():  # newest first, so the first one per key wins
            if (m := KEY_MARKER.search(issue["body"] or "")) and m[1] not in by_key:
                by_key[m[1]] = issue
    except urllib.error.HTTPError as e:
        if not dry:
            raise
        print(f"Cannot list issues ({e}), treating all problems as new")
    if not dry:
        gh.ensure_label()

    now = datetime.now(timezone.utc)
    for key, problem in sorted(problems.items()):
        title = problem_title(problem, gtfs)
        issue = by_key.get(key)
        if issue is None:
            body = render_body(problem, gtfs, run, None)
            new_issue = {"title": title, "body": body, "labels": [LABEL]}
            act(f"NEW   {title}", "POST", "issues", new_issue)
            continue

        number, texts = issue["number"], [issue["body"] or ""] + gh.comments(issue["number"])
        if issue["state"] == "open":
            seen = {s for t in texts for s in SIG_MARKER.findall(t)}
            if new := [e for e in problem.errors if e.sig not in seen]:
                body = f"New in {run}:\n\n" + render_errors(new, gtfs)
                act(f"NOTE  #{number} {title}", "POST", f"issues/{number}/comments", {"body": body})
            else:
                print(f"OPEN  #{number} {title}")
            continue

        if issue["state_reason"] in ("not_planned", "duplicate") and ignored(texts, now.date()):
            print(f"IGNORE #{number} {title}")
            continue

        closed_at = datetime.fromisoformat(issue["closed_at"].replace("Z", "+00:00"))
        if now - closed_at <= timedelta(days=REOPEN_DAYS):
            body = f"Back in {run}:\n\n" + render_errors(problem.errors, gtfs)
            act(f"REOPEN #{number} {title}", "PATCH", f"issues/{number}", {"state": "open"})
            act(f"       comment on #{number}", "POST", f"issues/{number}/comments", {"body": body})
        else:
            body = render_body(problem, gtfs, run, number)
            new_issue = {"title": title, "body": body, "labels": [LABEL]}
            act(f"NEW   {title} (was #{number})", "POST", "issues", new_issue)


def main() -> int:
    arg_parser = ArgumentParser(description=__doc__.partition("\n")[0])
    arg_parser.add_argument("log", help="build log, with or without ANSI colors and timestamps")
    arg_parser.add_argument("--gtfs", help="the warsaw.zip built alongside the log")
    arg_parser.add_argument("--run-url", default="", help="link to the build, for the issue text")
    arg_parser.add_argument("--dry-run", action="store_true", help="only print what would be done")
    args = arg_parser.parse_args()

    with open(args.log, encoding="utf-8", errors="replace") as f:
        problems = parse_log(f)
    print(f"{len(problems)} problem(s) in {args.log}")
    if not problems:
        return 0

    gtfs = Gtfs(args.gtfs, problems) if args.gtfs and os.path.exists(args.gtfs) else None
    run = f"[this build]({args.run_url})" if args.run_url else "a build"
    repo = os.environ.get("GITHUB_REPOSITORY", "mccartney/WarsawGTFS")
    token = os.environ.get("GITHUB_TOKEN")
    if not token and not args.dry_run:
        print("GITHUB_TOKEN is not set", file=sys.stderr)
        return 2
    sync(problems, GitHub(repo, token), gtfs, run, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
