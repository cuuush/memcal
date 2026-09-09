#!/usr/bin/env python3
"""Run the unit suite across dates, hours, and time zones to find clock dependencies."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import date, datetime, time as clock, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent.parent

#: `FAIL: test_x (tests.test_core.TestY.test_x)` — the parenthesised id is the one you
#: can paste back into `python3 -m unittest`.
OUTCOME_RE = re.compile(r"^(FAIL|ERROR): \S+ \(([^)\s]+)")


def default_dates(days: int) -> list[date]:
    """Every weekday between here and `days` out, then the far ones.

    A month, six months and a year are not padding: a literal date goes stale silently
    the moment it is behind today, and the failure that costs a morning is the one that
    arrives on a day nobody was running the suite.
    """
    today = date.today()
    near = [today + timedelta(days=n) for n in range(days + 1)]
    return near + [today + timedelta(days=n) for n in (30, 180, 365)]


#: Hours worth sweeping when `--hours` is given no values. Chosen for the boundaries
#: they straddle rather than for even spacing: 00 is "before the working day exists",
#: 09 is the reminder hour itself, 19 and 23 are either side of the end of waking hours,
#: which is where the failure that prompted this lived.
DEFAULT_HOURS = (0, 9, 15, 19, 23)


#: Zones worth sweeping when `--zones` is given no values. Three is enough to catch an
#: assumed offset: UTC is what CI runs at and what a hard-coded American offset breaks
#: on, and one zone either side of it catches a fixture that happens to work at UTC.
DEFAULT_ZONES = ("UTC", "Asia/Tokyo", "America/Los_Angeles")


def offset_changes(zone: str, days: list[date]) -> list[date]:
    """Days inside the swept span on which `zone`'s UTC offset changes.

    Derived rather than listed. A written-down DST date is exactly the kind of literal
    this tool exists to catch going stale, and the transitions move every year anyway.
    """
    try:
        info = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        return []
    first, last = min(days), max(days)
    changes, previous = [], None
    for step in range((last - first).days + 1):
        day = first + timedelta(days=step)
        now = datetime.combine(day, clock(12, 0), tzinfo=info).utcoffset()
        if previous is not None and now != previous:
            changes.append(day)
        previous = now
    return changes


def cross(days: list[date], hours: list, zones: list) -> list[tuple]:
    """The cheap shape: the two axes swept separately rather than multiplied.

    Measured on synthetic probes, a weekday bug and a zone bug are separable — the day
    walk in one zone catches the first and misses the second entirely, and one day in
    each zone catches the second. Multiplying them re-answers the same question thirty
    times. The exception is a bug that needs a particular day *and* a particular zone,
    which is what a DST transition is, so each zone's own transitions are swept in that
    zone rather than assumed away.
    """
    base, others = zones[0], zones[1:]
    moments = [(day, hour, base) for day in days for hour in hours]
    moments += [(days[0], hour, zone) for zone in others for hour in hours]
    for zone in zones:
        if zone is None:
            continue
        moments += [(day, hour, zone) for day in offset_changes(zone, days)
                    for hour in hours]
    return list(dict.fromkeys(moments))


def run(day: date, tests: list[str], hour: int | None = None,
        zone: str | None = None) -> tuple[int, list[str]]:
    pin = day.isoformat() if hour is None else f"{day.isoformat()}T{hour:02d}:00"
    env = {**os.environ, "MEMCAL_TODAY": pin, "PYTHONWARNINGS": "ignore"}
    if zone:
        env["TZ"] = zone
    cmd = [sys.executable, "-m", "unittest"]
    cmd += [f"tests.{t}" for t in tests] if tests else ["discover", "-s", "tests"]
    proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
    failed = sorted({m.group(2) for m in
                     (OUTCOME_RE.match(line) for line in proc.stderr.splitlines())
                     if m})
    return proc.returncode, failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=6,
                    help="how many days ahead to walk one at a time (default 6, a full "
                         "week of weekdays)")
    ap.add_argument("--dates", nargs="*", default=None,
                    help="explicit days to be, instead of the default walk")
    ap.add_argument("--tests", nargs="*", default=[],
                    help="dotted test ids under tests/, e.g. test_core.TestFoo")
    ap.add_argument("--hours", nargs="*", type=int, default=None,
                    help="also pin the hour, and run each day at each of these "
                         f"(bare --hours means {' '.join(map(str, DEFAULT_HOURS))})")
    ap.add_argument("--zones", nargs="*", default=None,
                    help="also run in each of these time zones "
                         f"(bare --zones means {', '.join(DEFAULT_ZONES)})")
    ap.add_argument("--cross", action="store_true",
                    help="sweep the day and zone axes separately instead of "
                         "multiplying them: the day walk in the first zone, one day in "
                         "each other zone, and each zone's own DST transitions. Implies "
                         "--zones. Roughly half the runs of the full grid")
    args = ap.parse_args()

    days = ([date.fromisoformat(d) for d in args.dates] if args.dates
            else default_dates(args.days))
    hours: list[int | None] = [None]
    if args.hours is not None:
        hours = list(args.hours) or list(DEFAULT_HOURS)
    zones: list[str | None] = [None]
    if args.zones is not None or args.cross:
        zones = list(args.zones or ()) or list(DEFAULT_ZONES)

    if args.cross:
        moments = cross(days, hours, zones)
        grid = len(days) * len(hours) * len(zones)
        print(f"cross: {len(moments)} run(s) instead of the grid's {grid}")
    else:
        moments = [(day, hour, zone)
                   for day in days for hour in hours for zone in zones]
    red: dict[str, list[str]] = {}
    for day, hour, zone in moments:
        code, failed = run(day, args.tests, hour, zone)
        when = (f"{day} {day.strftime('%a')}"
                + (f" {hour:02d}:00" if hour is not None else "")
                + (f" {zone}" if zone else ""))
        mark = "ok " if code == 0 else "RED"
        print(f"{mark} {when}  {'green' if not failed else str(len(failed)) + ' red'}")
        for test in failed:
            print(f"      {test}")
            red.setdefault(test, []).append(when)

    if red:
        axis = "moment" if (args.hours is not None or args.zones is not None) else "day"
        print(f"\n{len(red)} test(s) depend on the {axis} the suite runs:")
        for test, on in sorted(red.items(), key=lambda kv: -len(kv[1])):
            print(f"  {test}  ({len(on)}/{len(moments)} {axis}s, first {on[0]})")
        return 1
    print(f"\nall {len(moments)} moment(s) green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
