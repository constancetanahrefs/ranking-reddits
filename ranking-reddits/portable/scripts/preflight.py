#!/usr/bin/env python3
"""Pre-flight check — run this BEFORE your first scan.

Every call here is a FREE Ahrefs endpoint (no API units consumed), so you can
run it as often as you like. It verifies the key, the project, the keyword count
and your unit budget, then estimates what a scan will cost.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import ahrefs                    # noqa: E402
from app.config import config             # noqa: E402


def main() -> int:
    print("Ranking Reddits — pre-flight\n" + "=" * 30)
    missing = config.missing_required()
    if missing:
        print("Configuration incomplete:")
        for m in missing:
            print(f"  - {m}")
        return 2
    print("config: all required values present")

    try:
        usage = ahrefs.limits_and_usage()
        print(f"api key: OK — usage/limits: {str(usage)[:200]}")
    except Exception as e:                  # noqa: BLE001
        print(f"api key: FAILED — {e}")
        return 1

    n_kw = 0
    if "keywords" in config.sources:
        try:
            kws = ahrefs.project_keywords(config.rt_project_id)
            n_kw += len(kws)
            print(f"rank tracker project {config.rt_project_id}: {len(kws)} keywords")
            print(f"  sample: {', '.join(kws[:5])}")
        except Exception as e:              # noqa: BLE001
            print(f"rank tracker: FAILED — {e}")
            return 1

    if "brand" in config.sources:
        brand = ahrefs.expand_brand_keywords(config.brand_keywords, config.target_domain)
        n_kw += len(brand)
        print(f"brand keyword set: {len(brand)} keywords")
        print(f"  sample: {', '.join(brand[:5])}")

    capped = min(n_kw, config.max_keywords_per_scan)
    calls = capped * max(1, len(config.countries))
    print("\nestimated scan cost")
    print(f"  keywords: {capped} (cap {config.max_keywords_per_scan}) "
          f"x {len(config.countries)} market(s) = {calls} billable SERP calls")
    print(f"  minimum units: ~{calls * 50} (50 units is the floor per request; "
          f"actual cost scales with rows + fields)")
    print("\nAll checks above used FREE endpoints. Only the scan itself spends units.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
