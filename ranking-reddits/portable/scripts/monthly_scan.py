#!/usr/bin/env python3
"""Monthly scan — run from cron, a CI schedule, or any task runner.

    0 5 1 * *  cd /path/to/portable && python3 scripts/monthly_scan.py >> scan.log 2>&1

Idempotent: threads dedupe on the canonical thread key, hits on
(thread, keyword, country, source). Re-running the same month adds nothing.

Exits NON-ZERO if a source returned zero rows — a silent transport change must
never look like "nothing new this month".
"""
from __future__ import annotations

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine                    # noqa: E402
from app.config import config             # noqa: E402
from app.models import init_db            # noqa: E402


def main() -> int:
    missing = config.missing_required()
    if missing:
        print("FAIL config incomplete:")
        for m in missing:
            print(f"  - {m}")
        return 2

    init_db()
    jid = engine.job_new("monthly")
    try:
        engine.run_scan(jid, trigger="monthly")
    except Exception as exc:                # noqa: BLE001
        print(f"FAIL scan: {exc}")
        traceback.print_exc()
        return 1

    res = (engine.job_get(jid) or {}).get("result") or {}
    s = engine.stats()
    print(f"ok scan: {res.get('threads_seen', 0)} seen, {res.get('threads_new', 0)} new, "
          f"{res.get('hits_new', 0)} new hits | library: {s['total']} threads, "
          f"{s['new']} NEW, {s['unread']} unread")
    if not res.get("threads_seen"):
        print("FAIL zero rows — treated as a transport failure, not an empty month")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
