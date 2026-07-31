#!/usr/bin/env python3
"""Daily scan + email digest for Reddit Outpost (standalone build).

Run from the `portable/` directory:  python scripts/daily.py
Or on a cron:                        0 7 * * *  cd /app && python scripts/daily.py

For EACH watch profile: read every enabled subreddit's newest posts, score them
against that profile's brief, then email a digest to everyone who opted in.

Failure policy — a scan where NO subreddit could be read is a FAILURE, not a
quiet day on Reddit. Profiles are independent, so one failure doesn't abort the
others; the script exits non-zero naming whichever failed.

Reddit rate-limits this egress IP hard, so scanning is deliberately serial and
slow (roughly a minute per subreddit including retries). A 19-subreddit profile
takes ~20 minutes. That is expected, not a hang.
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select                       # noqa: E402

from app import engine as E                         # noqa: E402
from app.db import session_scope as cross_session_scope   # noqa: E402
from app.models import OutpostNotify                # noqa: E402

# Public URL of this app — used for the "Draft a reply" deep links in the digest.
APP_URL = os.environ.get("OUTPOST_APP_URL", "http://localhost:8000/")


def scan_profile(profile: dict) -> tuple[bool, str]:
    name = profile.get("name") or profile["id"][:8]
    gaps = E.missing_required(profile)
    if gaps:
        return True, f"SKIP {name}: incomplete — {'; '.join(gaps)}"
    if not E.list_subreddits(profile["id"], only_enabled=True):
        return True, f"SKIP {name}: no enabled subreddits"

    jid = E.job_new("daily")
    try:
        E.run_scan(jid, profile["id"], trigger="daily")
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return False, f"FAIL {name}: {exc}"

    j = E.job_get(jid) or {}
    if j.get("error"):
        return False, f"FAIL {name}: {j['error']}"
    r = j.get("result") or {}
    return True, (f"ok {name}: {r.get('subs_ok', 0)} subs, {r.get('posts_seen', 0)} seen, "
                  f"{r.get('posts_new', 0)} new, {r.get('matched', 0)} matched")


def _digest_html(profile: dict, items: list[dict]) -> str:
    rows = []
    for p in items:
        topics = " · ".join(t for t in (p.get("topics") or []))
        rel = f"{round((p.get('relevance') or 0) * 100)}%"
        brand = ('<span style="background:#fff3e0;color:#b45309;padding:1px 6px;'
                 'border-radius:4px;font-size:12px">names your brand</span>'
                 if p.get("brand_mention") else "")
        # Deep link: open the app focused on this card, drafting straight away.
        deep = f"{APP_URL}?p={profile['id']}&focus={p['reddit_id']}&autogen=1"
        body = (p.get("selftext") or "")[:280].replace("<", "&lt;")
        rows.append(f"""
        <div style="border:1px solid #e5e7eb;border-radius:8px;padding:12px;margin-bottom:10px">
          <div style="font-size:12px;color:#6b7280">
            r/{p['subreddit']} · {p.get('author') and 'u/' + p['author'] or ''} · {rel} match {brand}
          </div>
          <div style="font-size:15px;font-weight:600;margin:4px 0">
            <a href="{p['url']}" style="color:#111827;text-decoration:none">{p['title']}</a>
          </div>
          {f'<div style="font-size:12px;color:#6b7280">{topics}</div>' if topics else ''}
          {f'<div style="font-size:13px;color:#374151;margin-top:4px">{p["reasoning"]}</div>' if p.get('reasoning') else ''}
          {f'<div style="font-size:12px;color:#6b7280;margin-top:4px">{body}…</div>' if body else ''}
          <div style="margin-top:8px">
            <a href="{deep}" style="background:#111827;color:#fff;padding:6px 12px;
               border-radius:6px;text-decoration:none;font-size:13px">Draft a reply</a>
            <a href="{p['url']}" style="color:#6b7280;font-size:13px;margin-left:10px">Open on Reddit</a>
          </div>
        </div>""")
    return f"""<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:640px">
      <h2 style="font-size:18px;margin-bottom:2px">Reddit Outpost — {profile.get('name')}</h2>
      <p style="color:#6b7280;font-size:13px;margin-top:0">
        {len(items)} thread(s) worth a look from the last day.
        You write the replies — this app never posts.</p>
      {''.join(rows)}
      <p style="color:#9ca3af;font-size:12px">
        <a href="{APP_URL}?p={profile['id']}" style="color:#6b7280">Open Reddit Outpost</a>
        · turn these off on the Notifications tab</p>
    </div>"""


def send_digests(profile: dict) -> str:
    """Email everyone who opted in. Never blocks the scan on a mail failure."""
    with cross_session_scope() as s:
        subs = [r.to_dict() for r in s.execute(
            select(OutpostNotify).where(OutpostNotify.enabled.is_(True))).scalars().all()]
    if not subs:
        return "no digest recipients"

    sent, failed = 0, []
    try:
        from app import notify as letaido_email
    except Exception as exc:  # noqa: BLE001
        return f"digest skipped — mail backend unavailable: {exc}"

    for cfg in subs:
        items = E.digest_items(profile["id"], since_hours=26,
                               matched_only=cfg.get("matched_only", True))
        if not items:
            continue
        # Recipient, in order: explicit override, then the address captured from
        # their Console session at opt-in time. `user_id` is a UUID, never an
        # email — using it as a fallback silently skipped everyone.
        # Standing alone there is no login, so OUTPOST_EMAIL is the fallback.
        to = ((cfg.get("email_override") or "").strip()
              or (cfg.get("session_email") or "").strip()
              or os.environ.get("OUTPOST_EMAIL", "").strip())
        if "@" not in to:
            failed.append(f"{cfg['user_id'][:8]}: no address — set OUTPOST_EMAIL "
                          "or an override on the Notifications tab")
            continue
        try:
            letaido_email.send_email(
                to=to,
                subject=f"Reddit Outpost — {len(items)} thread(s) for {profile.get('name')}",
                body="\n\n".join(f"r/{i['subreddit']} — {i['title']}\n{i['url']}"
                                 for i in items),
                body_html=_digest_html(profile, items))
            sent += 1
            with cross_session_scope() as s:
                row = s.get(OutpostNotify, cfg["user_id"])
                if row:
                    row.last_notified_at = datetime.now(timezone.utc)
                    row.notify_count = (row.notify_count or 0) + 1
                    row.last_error = ""
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{to}: {type(exc).__name__} {str(exc)[:120]}")
            with cross_session_scope() as s:
                row = s.get(OutpostNotify, cfg["user_id"])
                if row:
                    row.last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
    msg = f"{sent} digest(s) sent"
    if failed:
        msg += " | failed: " + "; ".join(failed)
    return msg


def main() -> int:
    E.recover_stale_runs()
    try:
        profiles = E.list_profiles()
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL could not list profiles: {exc}")
        traceback.print_exc()
        return 1
    if not profiles:
        print("FAIL no watch profiles configured")
        return 1

    failures = []
    for prof in profiles:
        ok, line = scan_profile(prof)
        print(line, flush=True)
        if not ok:
            failures.append(prof.get("name") or prof["id"][:8])
            continue
        if line.startswith("ok"):
            try:
                print("   " + send_digests(prof), flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"   digest error: {exc}", flush=True)
            try:
                swept = E.retention_sweep(prof["id"])
                if swept:
                    print(f"   retention: removed {swept} old post(s)", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"   retention error: {exc}", flush=True)

    print(f"— {len(profiles)} profile(s), {len(failures)} failed")
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
