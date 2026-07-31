"""Ahrefs API v3 client — only the calls this app needs.

Endpoint reference and cost notes: docs/API_MAPPING.md.
Free endpoints (no API units): /management/*, /rank-tracker/*, /subscription-info/*.
Billable: /serp-overview (>=50 units per call), /keywords-explorer/*.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import config


class AhrefsError(RuntimeError):
    pass


def _get(path: str, params: dict[str, Any] | None = None, *, timeout: int = 60) -> dict:
    if not config.ahrefs_api_key:
        raise AhrefsError(
            "No AHREFS_API_KEY set. Create one at https://app.ahrefs.com/account/api-keys "
            "(requires a paid plan, Lite or above).")
    qs = urllib.parse.urlencode({k: v for k, v in (params or {}).items()
                                 if v not in (None, "", [])}, doseq=True)
    url = f"{config.ahrefs_base}{path}" + (f"?{qs}" if qs else "")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {config.ahrefs_api_key}",
        "Accept": "application/json",
    })
    last = ""
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                import json
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:400]
            except Exception:  # noqa: BLE001
                pass
            if e.code == 429:                      # documented rate limit ~60/min
                time.sleep(5 * (attempt + 1))
                last = "rate limited (429)"
                continue
            if e.code == 401:
                raise AhrefsError("Ahrefs rejected the API key (401). Check AHREFS_API_KEY.")
            if e.code == 403:
                raise AhrefsError(
                    "Ahrefs returned 403 — your plan may lack API access or this "
                    f"endpoint's entitlement, or units are exhausted. {body}")
            raise AhrefsError(f"{path} -> HTTP {e.code}: {body}")
        except Exception as e:                     # noqa: BLE001
            last = str(e)[:200]
            time.sleep(2 * (attempt + 1))
    raise AhrefsError(f"{path} failed after retries: {last}")


# ── Free endpoints ────────────────────────────────────────────────────────

def limits_and_usage() -> dict:
    """Free. Call before a big scan so you can tell the user the cost."""
    return _get("/subscription-info/limits-and-usage")


def list_projects() -> list[dict]:
    """Free. GET /v3/management/projects — let the user PICK, don't ask for an id."""
    res = _get("/management/projects")
    return res.get("projects") or []


def project_keywords(project_id: str) -> list[str]:
    """Free. GET /v3/management/project-keywords.

    A zero-keyword answer is a FAILURE, not an empty success — an empty project
    and a broken transport must not look the same.
    """
    res = _get("/management/project-keywords", {"project_id": project_id})
    rows = res.get("keywords") or []
    kws = sorted({(r.get("keyword") or "").strip() for r in rows if r.get("keyword")})
    if not kws:
        raise AhrefsError(
            f"Rank Tracker project {project_id} returned 0 keywords. Either the project "
            f"tracks nothing yet, or the id/API key don't match. Verify with "
            f"GET /v3/management/project-keywords?project_id={project_id} (free).")
    return kws


def rank_tracker_overview(project_id: str, limit: int = 1000) -> list[dict]:
    """Free. Richer per-keyword rows (position, volume, tags, ranking URL).

    Used for tag filtering: tags come back per keyword and are filtered client-side.
    """
    res = _get("/rank-tracker/overview", {
        "project_id": project_id,
        "select": "keyword,position,volume,tags,url,country_code",
        "limit": limit,
    })
    return res.get("keywords") or res.get("rows") or res.get("records") or []


def brand_radar_reports() -> list[dict]:
    """Free. Optional — only for the AI-citations extra."""
    res = _get("/management/brand-radar-reports")
    return res.get("reports") or []


# ── Billable ──────────────────────────────────────────────────────────────

SERP_SELECT = ("position,result_type,url,title,description,domain_rating,"
               "traffic,top_keyword,top_volume")


def serp_overview(keyword: str, country: str = "us", top_positions: int = 10) -> list[dict]:
    """Billable (>=50 units). GET /v3/serp-overview — the top organic results.

    `top_positions` is passed through so you don't pay for rows you'd discard.
    """
    res = _get("/serp-overview", {
        "keyword": keyword,
        "country": country,
        "select": SERP_SELECT,
        "top_positions": top_positions,
    })
    rows = res.get("positions") or res.get("records") or res.get("organic") or []
    return rows


def reddit_rows_for_keyword(keyword: str, country: str, max_position: int) -> list[dict]:
    """SERP Overview filtered to reddit.com results inside the position cap.

    This is the portable replacement for the Letaido `reddit_results` connector —
    the public API has no Reddit-in-SERPs endpoint (docs/API_MAPPING.md §2).
    """
    out: list[dict] = []
    for row in serp_overview(keyword, country, max_position):
        url = row.get("url") or ""
        if "reddit.com" not in url:
            continue
        pos = row.get("position")
        if pos is not None and int(pos) > max_position:
            continue
        out.append({
            "url": url,
            "title": row.get("title") or "",
            "description": row.get("description") or "",
            "serp_position": pos,
            "search_volume": row.get("top_volume"),
            "keyword": keyword,
            "country": country,
        })
    return out


def expand_brand_keywords(seeds: list[str], target_domain: str) -> list[str]:
    """Brand keyword set for the brand source.

    Kept intentionally cheap and predictable: the seeds plus common commercial
    modifiers. Uncomment the matching-terms call to widen it — that endpoint
    CONSUMES UNITS, so it's opt-in rather than automatic.
    """
    mods = ["", " pricing", " review", " alternative", " alternatives",
            " vs", " discount", " free trial", " worth it"]
    out: set[str] = set()
    for s in seeds:
        s = s.strip()
        if not s:
            continue
        for m in mods:
            out.add((s + m).strip())
    if target_domain:
        out.add(target_domain)
    return sorted(out)

    # Wider (billable) expansion, if you want it:
    # res = _get("/keywords-explorer/matching-terms",
    #            {"keywords": ",".join(seeds), "country": "us",
    #             "select": "keyword,volume", "limit": 100})
