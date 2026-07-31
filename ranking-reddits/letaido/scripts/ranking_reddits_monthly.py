#!/usr/bin/env python3
"""Monthly scan for the Ranking Reddits Console app — every project.

For EACH configured project (workspace) it runs both discovery sources
(Brand Radar SERP visibility + Reddit pages in the top-N for that project's
tracked Rank Tracker keywords), upserts every thread, and flags genuinely-new
URLs as NEW so they show up as new cards in that project's wall.

Idempotent: threads dedupe on (project, /comments/<id> slug), hits dedupe on
(thread, keyword, country, source). Re-running the same month is free.

Failure policy — a scan that comes back with zero SERP rows is a FAILURE, not
an empty month: a silent transport change must never look like "nothing new".
Projects are independent, so one bad project doesn't abort the others; the
script exits NON-ZERO if ANY project failed, and the log names which.

Projects still missing required config are SKIPPED (not failed) — they've
simply never finished the setup wizard.
"""
from __future__ import annotations

import sys
import traceback

sys.path.insert(0, "/home/console/http/default")

import applications._ranking_reddits_engine as E   # noqa: E402


def scan_one(ws: dict) -> tuple[bool, str]:
    """Scan a single project. Returns (ok, one-line summary)."""
    name = ws.get("name") or ws["id"][:8]

    gaps = E.missing_required(ws)
    if gaps:
        return True, f"SKIP {name}: not configured — {'; '.join(gaps)}"

    jid = E.job_new("monthly")
    try:
        E.run_scan(jid, ws_id=ws["id"], trigger="monthly")
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return False, f"FAIL {name}: {exc}"

    j = E.job_get(jid) or {}
    if j.get("error"):
        return False, f"FAIL {name}: {j['error']}"

    res = j.get("result") or {}
    st = E.stats(ws["id"])
    line = (f"{name}: {res.get('threads_seen', 0)} threads seen, "
            f"{res.get('threads_new', 0)} new, {res.get('hits_new', 0)} new keyword hits "
            f"| library: {st['total']} threads, {st['new']} flagged NEW, "
            f"{st['unread']} unread")

    if not res.get("threads_seen"):
        return False, ("FAIL " + line
                       + " — zero threads seen is treated as a transport failure, "
                         "not an empty month")
    return True, "ok " + line


def main() -> int:
    try:
        workspaces = E.list_workspaces()
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL could not list projects: {exc}")
        traceback.print_exc()
        return 1

    if not workspaces:
        print("FAIL no projects configured — nothing to scan")
        return 1

    failures = []
    for ws in workspaces:
        ok, line = scan_one(ws)
        print(line, flush=True)
        if not ok:
            failures.append(ws.get("name") or ws["id"][:8])

    print(f"— {len(workspaces)} project(s) processed, {len(failures)} failed")
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
