#!/usr/bin/env python3
"""Show reproduction gaps and relationships in the open issue queue.

    python3 tools/triage.py
    python3 tools/triage.py --needs-reproduction
    python3 tools/triage.py --issue 36

The tool reports facts the tracker can prove. It does not grade, rank, or classify issues.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys


def issues(state: str = "open") -> list[dict]:
    out = subprocess.run(
        ["gh", "issue", "list", "--state", state, "--limit", "300",
         "--json", "number,title,body,labels"],
        capture_output=True, text=True)
    if out.returncode:
        sys.exit(f"gh failed: {out.stderr.strip()}")
    return json.loads(out.stdout)


def label_names(issue: dict) -> set[str]:
    return {label["name"] for label in issue["labels"]}


def measure(rows: list[dict]) -> dict[int, dict]:
    numbers = {issue["number"] for issue in rows}
    cites: dict[int, list[int]] = {}
    for issue in rows:
        found = {int(value) for value in re.findall(r"#(\d+)", issue.get("body") or "")}
        found.discard(issue["number"])
        cites[issue["number"]] = sorted(found & numbers)
    cited_by = {
        number: sorted(source for source, targets in cites.items() if number in targets)
        for number in numbers
    }

    return {
        issue["number"]: {
            "issue": issue,
            "needs_reproduction": "needs-reproduction" in label_names(issue),
            "areas": sorted(name for name in label_names(issue) if name.startswith("area:")),
            "kind": next((name for name in label_names(issue) if name.startswith("kind:")), ""),
            "cites": cites[issue["number"]],
            "cited_by": cited_by[issue["number"]],
        }
        for issue in rows
    }


def _line(row: dict) -> str:
    issue = row["issue"]
    notes = []
    if row["kind"]:
        notes.append(row["kind"].removeprefix("kind:"))
    if row["areas"]:
        notes.append(", ".join(area.removeprefix("area:") for area in row["areas"]))
    suffix = f"  [{' · '.join(notes)}]" if notes else ""
    return f"#{issue['number']} {issue['title']}{suffix}"


def render(measured: dict[int, dict], *, only_needs: bool = False,
           one: int | None = None) -> None:
    rows = list(measured.values())
    if one is not None:
        rows = [row for row in rows if row["issue"]["number"] == one]
        if not rows:
            sys.exit(f"#{one} is not an open issue")
    if only_needs:
        rows = [row for row in rows if row["needs_reproduction"]]
    if not rows:
        print("nothing to show")
        return

    if one is not None:
        row = rows[0]
        print(_line(row))
        print(f"needs reproduction: {'yes' if row['needs_reproduction'] else 'no'}")
        print(f"references: {row['cites'] or '—'}")
        print(f"referenced by: {row['cited_by'] or '—'}")
        return

    needs = [row for row in rows if row["needs_reproduction"]]
    referenced = [row for row in rows if not row["needs_reproduction"] and row["cited_by"]]
    rest = [row for row in rows if not row["needs_reproduction"] and not row["cited_by"]]

    groups = (
        ("NEEDS REPRODUCTION", needs),
        ("REFERENCED BY OTHER OPEN ISSUES", referenced),
        ("OTHER OPEN WORK", rest),
    )
    for heading, group in groups:
        if not group:
            continue
        print(f"\n{heading}")
        group.sort(key=lambda row: (-len(row["cited_by"]), -row["issue"]["number"]))
        for row in group:
            print(f"  {_line(row)}")
            if row["cited_by"]:
                print("    referenced by " + ", ".join(f"#{n}" for n in row["cited_by"]))

    print(f"\n{len(measured)} open · {sum(r['needs_reproduction'] for r in measured.values())} "
          "need reproduction")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--needs-reproduction", action="store_true",
                        help="show only issues whose mechanism still needs proof")
    parser.add_argument("--issue", type=int, help="show one issue and its references")
    args = parser.parse_args()
    render(measure(issues()), only_needs=args.needs_reproduction, one=args.issue)


if __name__ == "__main__":
    main()
