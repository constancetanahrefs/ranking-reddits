"""Configuration — every account-specific value comes from the environment.

Deliberately NO working defaults for account values: a placeholder domain or a
borrowed project id would silently scan the wrong account. `missing_required()`
is what the UI and the scan endpoint use to refuse to run and say why.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _csv(name: str) -> list[str]:
    return [x.strip() for x in (os.getenv(name) or "").split(",") if x.strip()]


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


@dataclass
class Config:
    # ── Ahrefs ────────────────────────────────────────────────────────────
    ahrefs_api_key: str = field(default_factory=lambda: os.getenv("AHREFS_API_KEY", ""))
    ahrefs_base: str = field(default_factory=lambda: os.getenv(
        "AHREFS_API_BASE", "https://api.ahrefs.com/v3"))
    rt_project_id: str = field(default_factory=lambda: os.getenv("RT_PROJECT_ID", ""))
    rt_tags: list[str] = field(default_factory=lambda: _csv("RT_TAGS"))
    brand_radar_report_id: str = field(
        default_factory=lambda: os.getenv("BRAND_RADAR_REPORT_ID", ""))

    # ── Scope ─────────────────────────────────────────────────────────────
    target_domain: str = field(default_factory=lambda: os.getenv("TARGET_DOMAIN", ""))
    brand_keywords: list[str] = field(default_factory=lambda: _csv("BRAND_KEYWORDS"))
    countries: list[str] = field(default_factory=lambda: [
        c.lower() for c in _csv("COUNTRIES")] or ["us"])
    max_serp_position: int = field(default_factory=lambda: _int("MAX_SERP_POSITION", 10))
    sources: list[str] = field(default_factory=lambda: _csv("SOURCES") or ["brand", "keywords"])
    # Hard ceiling on billable SERP calls per scan. Protects the user's API units.
    max_keywords_per_scan: int = field(
        default_factory=lambda: _int("MAX_KEYWORDS_PER_SCAN", 250))

    # ── Storage ───────────────────────────────────────────────────────────
    database_url: str = field(default_factory=lambda: os.getenv(
        "DATABASE_URL", "postgresql+psycopg2://localhost/ranking_reddits"))

    # ── LLM (optional — no key means no AI notes, app still works) ─────────
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    llm_base_url: str = field(default_factory=lambda: os.getenv(
        "LLM_BASE_URL", "https://api.openai.com/v1"))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4o-mini"))

    # ── Reddit enrichment ─────────────────────────────────────────────────
    reddit_user_agent: str = field(default_factory=lambda: os.getenv(
        "REDDIT_USER_AGENT", "ranking-reddits/1.0 (+https://github.com/)"))
    max_comments: int = field(default_factory=lambda: _int("MAX_COMMENTS", 15))
    # Optional — upvote counts only. Everything else works without it.
    apify_api_token: str = field(default_factory=lambda: os.getenv("APIFY_API_TOKEN", ""))
    apify_render_actor: str = field(default_factory=lambda: os.getenv(
        "APIFY_RENDER_ACTOR", "apify~website-content-crawler"))

    # ── Behaviour ─────────────────────────────────────────────────────────
    auto_fetch_on_open: bool = field(default_factory=lambda: _bool("AUTO_FETCH_ON_OPEN", True))
    auto_notes_on_open: bool = field(default_factory=lambda: _bool("AUTO_NOTES_ON_OPEN", True))

    # ── Validation ────────────────────────────────────────────────────────
    def missing_required(self) -> list[str]:
        """Which required values are absent, phrased for a human."""
        out: list[str] = []
        if not self.ahrefs_api_key:
            out.append("AHREFS_API_KEY — create one at "
                       "https://app.ahrefs.com/account/api-keys (paid plan, Lite or above)")
        if "keywords" in self.sources and not self.rt_project_id:
            out.append("RT_PROJECT_ID — list your projects with "
                       "GET /v3/management/projects (free) and pick one")
        if "brand" in self.sources and not self.brand_keywords:
            out.append("BRAND_KEYWORDS — comma-separated brand terms, e.g. 'acme,acme crm'")
        if "brand" in self.sources and not self.target_domain:
            out.append("TARGET_DOMAIN — the domain you're monitoring, e.g. 'example.com'")
        if not self.sources:
            out.append("SOURCES — at least one of 'brand', 'keywords'")
        return out

    @property
    def notes_enabled(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def votes_enabled(self) -> bool:
        return bool(self.apify_api_token)


config = Config()
