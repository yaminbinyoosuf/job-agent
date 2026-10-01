#!/usr/bin/env python3
"""
Automated job search agent for Yamin Binyoosuf.

Every run it:
  1. Pulls several free, key-less remote job feeds (RemoteOK, WeWorkRemotely,
     Remotive, Arbeitnow, Jobicy, Hacker News "Who is hiring?").
  2. Normalizes + de-duplicates them, keeps postings inside the lookback
     window, and applies a fast keyword pre-filter (title-level blocklist for
     clearly irrelevant roles, then a positive keyword match).
  3. Scores each remaining posting 1-10 with DeepSeek and drafts a
     personalized outreach email grounded in Yamin's real resume.
  4. For anything scoring >= SCORE_THRESHOLD it emails the draft + apply link
     (and the hiring contact, when the posting publishes one) to NOTIFY_EMAIL.
     With AUTO_APPLY=true it additionally sends the outreach straight to a
     contact address that the posting itself published.
  5. Logs every posting it looked at to `jobs_log.csv` so later runs skip it.

Required environment variables:
    DEEPSEEK_API_KEY    DeepSeek API key (primary engine)
    RESEND_API_KEY      Resend API key (email delivery)

Optional environment variables:
    DEEPSEEK_MODEL      Default: deepseek-chat. deepseek-v4-pro and
                        deepseek-flash are reasoning models (slower, more
                        tokens, generally stronger judgement).
    DEEPSEEK_MAX_TOKENS Default: 8000. Must stay high for reasoning models,
                        which spend the budget on reasoning before answering.
    RESEND_FROM         Default: onboarding@resend.dev (see README)
    NOTIFY_EMAIL        Default: yaminbinyoosuf@gmail.com
    GEMINI_API_KEY      Only used as a fallback if DEEPSEEK_API_KEY is absent
    COGEXT_API_KEY      Optional commitment tracking (cogextai.com)
    AUTO_APPLY          "true" to email published hiring contacts directly
    DRY_RUN             "true" to score + log but never send email
    MAX_JOBS_PER_RUN    Cap on postings scored per run (default 40)

Usage:
    python job_agent.py                # normal run
    python job_agent.py --self-test    # verify DeepSeek + Resend wiring
    python job_agent.py --dry-run      # score + log, send nothing
"""

from __future__ import annotations

import ast
import csv
import html
import json
import os
import re
import shutil
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable

import requests

# google-genai is optional: it is only needed for the Gemini fallback path.
try:
    from google import genai as _genai  # type: ignore
except ImportError:  # pragma: no cover
    _genai = None  # type: ignore

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

USER_AGENT = "Mozilla/5.0 (compatible; job-agent/2.0; +https://github.com/yaminbinyoosuf/job-agent)"

REMOTEOK_API_URL = "https://remoteok.com/api"
WWR_RSS_URLS = [
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
]
REMOTIVE_API_URL = "https://remotive.com/api/remote-jobs"
ARBEITNOW_API_URL = "https://www.arbeitnow.com/api/job-board-api"
JOBICY_API_URL = "https://jobicy.com/api/v2/remote-jobs?count=50"
HIMALAYAS_API_URL = "https://himalayas.app/jobs/api?limit=100"
WORKINGNOMADS_API_URL = "https://www.workingnomads.com/api/exposed_jobs/"
# Task-based / no-interview sources.
MERCOR_JOBS_URL = "https://www.mercor.com/jobs"
FREELANCER_API_URL = (
    "https://www.freelancer.com/api/projects/0.1/projects/active/"
    "?limit=100&job_details=true&full_description=true&sort_field=time_updated"
)
HN_FREELANCER_SEARCH_URL = (
    "https://hn.algolia.com/api/v1/search_by_date"
    "?query=%22Ask%20HN%3A%20Freelancer%3F%20Seeking%20freelancer%22&tags=story&hitsPerPage=5"
)
HN_SEARCH_URL = (
    "https://hn.algolia.com/api/v1/search_by_date"
    "?query=%22Ask%20HN%3A%20Who%20is%20hiring%22&tags=story&hitsPerPage=5"
)
HN_MAX_PAGES = 3  # 100 comments per page

# --- Tuning knobs -----------------------------------------------------------

# Positive signal: matched against title + description + tags.
KEYWORDS = [
    "ai agent",
    "agentic",
    "llm",
    "genai",
    "generative ai",
    "rag",
    "openai",
    "langchain",
    "prompt engineer",
    "ai engineer",
    "ml engineer",
    "machine learning",
    "fastapi",
    "python",
    "whatsapp",
    "voice agent",
    "automation",
    "backend",
    "back-end",
    "api",
    "postgres",
    "docker",
    "webhook",
    "integration",
    "full-stack",
    "fullstack",
    "software engineer",
    "software developer",
    "typescript",
    "react",
    "n8n",
    "workflow",
    "telegram bot",
]

# Negative signal: matched against the TITLE only. Keeps obviously
# non-engineering roles from burning API calls and quota.
TITLE_BLOCKLIST = [
    "sales",
    "account executive",
    "account manager",
    "business development",
    "marketing",
    "growth",
    "demand gen",
    "seo",
    "social media",
    "copywriter",
    "content writer",
    "content reviewer",
    "editor",
    "accountant",
    "bookkeep",
    "payroll",
    "tax ",
    "audit",
    "recruiter",
    "talent acquisition",
    "human resources",
    "people partner",
    "customer support",
    "customer service",
    "customer success",
    "call center",
    "virtual assistant",
    "data entry",
    "transcri",
    "translator",
    "interpreter",
    "nurse",
    "clinical",
    "physician",
    "therapist",
    "tutor",
    "teacher",
    "insurance",
    "mortgage",
    "real estate",
    "attorney",
    "paralegal",
    "sap ",
    "workday",
    "netsuite",
    "salesforce admin",
    "gis ",
    "salesforce consultant",
    "manager",
    # Seniority well beyond this candidate: these never convert.
    "principal",
    "staff engineer",
    "staff software",
    "staff product",
    "staff security",
    "staff data",
    "staff machine",
    "distinguished",
    "director",
    "head of",
    "vp ",
    "vice president",
    "chief ",
    "engineering manager",
    "architect",
    "fellow",
    # Non-engineering functions that still slip through on keyword matches.
    "analyst",
    "specialist",
    "consultant",
    "advisor",
    "strategist",
    "coordinator",
    "representative",
    "administrator",
    "instructional",
    "curriculum",
    "content developer",
    "content manager",
    "communications",
    "public relations",
    "designer",
    "ux ",
    " ui ",
    # Mercor carries a lot of non-software domain-expert work.
    "physicist",
    "mechanical",
    "civil engineer",
    "chemical",
    "biolog",
    "pharmac",
    "dentist",
    "veterinar",
    "geolog",
]

# German-language postings dominate Arbeitnow and almost never fit. Matches
# the (m/w/d) style gender marker and a few unmistakable German words.
GERMAN_TITLE_RE = re.compile(
    r"\((?:m|w|f|d|x)\s*/\s*(?:m|w|f|d|x)(?:\s*/\s*(?:m|w|f|d|x))?\)"
    r"|werkstudent|praktikant|berater|finanz|immobilien|mitarbeiter|für|"
    r"ausbildung|bewerbung",
    re.I,
)

SCORE_THRESHOLD = int(os.environ.get("SCORE_THRESHOLD") or 6)
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS") or 72)
# Standing task/contract boards (Mercor, Freelancer.com) are catalogs of work
# that is still open, not a stream of new postings — a Mercor listing from a
# month ago is usually still accepting applicants. Applying the freshness
# window that suits job feeds would discard essentially all of it.
LOOKBACK_HOURS_TASK = int(os.environ.get("LOOKBACK_HOURS_TASK") or 24 * 90)
MAX_JOBS_PER_RUN = int(os.environ.get("MAX_JOBS_PER_RUN") or 40)

# --- Task / no-interview targeting -----------------------------------------

# Only email postings that are task or contract work, or that the model
# judges to carry no interview. This is the whole point for a candidate who
# will not sit interviews.
NO_INTERVIEW_ONLY = (os.environ.get("NO_INTERVIEW_ONLY") or "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
# Freelancer.com noise floor: skip tiny budgets and projects already buried
# under a pile of bids, which nobody wins.
FREELANCE_MIN_BUDGET = float(os.environ.get("FREELANCE_MIN_BUDGET") or 50)
FREELANCE_MAX_BIDS = int(os.environ.get("FREELANCE_MAX_BIDS") or 120)
# Remotive/Jobicy/etc. rate employment types like this; anything matching is
# treated as contract rather than full-time.
CONTRACT_TYPE_HINTS = ("contract", "contractor", "freelance", "temporary", "part_time")
# Task work is surfaced before contract, which is surfaced before full-time.
WORK_TYPE_RANK = {"task": 0, "contract": 1, "full_time": 2}

# --- Engines ----------------------------------------------------------------

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY") or ""
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL") or "deepseek-chat"
# Stronger model used ONLY to rewrite the outreach for scoring matches, so the
# bulk scoring pass stays cheap and fast. Set to "" to disable the second pass.
DEEPSEEK_DRAFT_MODEL = (
    os.environ.get("DEEPSEEK_DRAFT_MODEL")
    if os.environ.get("DEEPSEEK_DRAFT_MODEL") is not None
    else "deepseek-v4-pro"
)
DEEPSEEK_MAX_TOKENS = int(os.environ.get("DEEPSEEK_MAX_TOKENS") or 8000)

GEMINI_MODEL_ENV = os.environ.get("GEMINI_MODEL") or ""
GEMINI_MODEL_PREFERENCES = [
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
]
GEMINI_MIN_INTERVAL_S = 13  # Free tier: 5 req/min. 60 / 5 = 12s; add margin.

# --- Delivery ---------------------------------------------------------------

RESEND_API_URL = "https://api.resend.com/emails"
RESEND_FROM = os.environ.get("RESEND_FROM") or "onboarding@resend.dev"
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL") or "yaminbinyoosuf@gmail.com"
AUTO_APPLY = (os.environ.get("AUTO_APPLY") or "").strip().lower() in {"1", "true", "yes", "on"}
DRY_RUN = (os.environ.get("DRY_RUN") or "").strip().lower() in {"1", "true", "yes", "on"}
# Only genuinely strong matches get a direct email to the company.
AUTO_APPLY_MIN_SCORE = int(os.environ.get("AUTO_APPLY_MIN_SCORE") or 8)
# Where a hiring manager's reply should land. Without this, replies to mail
# sent from a shared sender domain never reach Yamin.
OUTREACH_REPLY_TO = os.environ.get("OUTREACH_REPLY_TO") or os.environ.get(
    "NOTIFY_EMAIL"
) or "yaminbinyoosuf@gmail.com"


def outreach_sender_ready() -> tuple[bool, str]:
    """Direct outreach needs a sender on a domain the user controls.

    Resend's shared onboarding@resend.dev sender can only deliver to the
    account owner, and even where it is accepted a reply would go to Resend
    rather than to Yamin — which defeats the point of the email."""
    if DRY_RUN:
        return False, "DRY_RUN is on"
    if RESEND_FROM.strip().lower().endswith("@resend.dev"):
        return False, (
            "RESEND_FROM is still the shared resend.dev test sender. Verify a domain "
            "at resend.com/domains, then set the RESEND_FROM secret to an address on "
            "it (e.g. yamin@thryvixai.com). Direct outreach stays off until then."
        )
    return True, "ready"

COGEXT_API_KEY = os.environ.get("COGEXT_API_KEY") or ""
COGEXT_API_URL = "https://api.cogextai.com/api/v1"
COGEXT_AGENT_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"

# --- Log --------------------------------------------------------------------

LOG_PATH = Path(__file__).parent / "jobs_log.csv"
LOG_FIELDS = [
    "timestamp",
    "job_id",
    "source",
    "title",
    "company",
    "url",
    "score",
    "emailed",
    "reason",
    "rubric",
]

# Bump this whenever the scoring prompt, rubric or scoring model changes in a
# way that could move a score. Rows logged under an older rubric and scored
# below the threshold are re-evaluated once, so an improved rubric can rescue
# postings the previous one underrated. Anything already emailed is never
# re-sent, whatever the rubric.
RUBRIC_VERSION = "2026-10-02.1"

# ---------------------------------------------------------------------------
# Candidate profile (built from Yamin_Bin_Yoosuf_Mercor_Final_Resume.docx)
# ---------------------------------------------------------------------------

PROFILE = """\
Name: Yamin Binyoosuf
Location: Malappuram, Kerala, India. Remote-only, available immediately.
Contact: yaminbinyoosuf@gmail.com | +91 8304881059
Links: thryvixai.com | cogextai.com | github.com/yaminbinyoosuf | linkedin.com/in/yaminbinyoosuf
Rate: $25-50/hr, or fixed-price project work.
Education: Higher Secondary Education, Computer Science (Kerala, 2023). Self-taught engineer, no degree.

SUMMARY
Self-taught software engineer and solo founder who builds and deploys production
AI systems end-to-end, from architecture through deployment, testing, debugging
and production operations. Two independent products shipped and running.

TECHNICAL SKILLS
Languages: Python, TypeScript, JavaScript, SQL
Backend: FastAPI, Pydantic, SQLAlchemy, REST APIs, Async Python
Frontend: React, Vite, TypeScript, JavaScript
Databases: PostgreSQL, Supabase
AI/LLM: DeepSeek, Groq, Llama 3.3 70B, Sarvam AI, structured LLM extraction,
  JSON-mode prompting, prompt engineering, RAG, confidence scoring, validation
Cloud/DevOps: AWS Lightsail, Docker, Nginx, Cloudflare, Vercel, GitHub Actions, Linux
Integrations: WhatsApp Cloud API, Razorpay, Google Places API, Gmail, Telegram
Engineering: API design, state machines, event logging, webhooks (HMAC-SHA256,
  SSRF protection), authentication, automated testing, CI/CD, security, PII redaction

EXPERIENCE
1) Founder & Software Engineer - THRYVIX AI (2025-Present)
   Production clinic operating system, built and operated solo.
   - FastAPI + SQLAlchemy + PostgreSQL backend services.
   - React/Vite front-ends for operational and visitor-facing workflows.
   - Appointment and queue infrastructure: QR booking, walk-ins, live queue
     tracking, dynamic ETA prediction, appointment lifecycle management.
   - WhatsApp Cloud API automation in English and Malayalam.
   - Sarvam AI integration for Malayalam voice-based booking.
   - No-show prediction, pre-consultation briefs, Health IDs, family booking,
     payment links, visitor tracking, weekly intelligence reports.
   - Deployed with AWS Lightsail, Docker, Nginx, Cloudflare, SSL.
   - Built an automated outreach system with Python, Google Places API,
     Playwright, Gmail, Google Sheets, Telegram, GitHub Actions and DeepSeek,
     including LLM lead qualification, fake-email detection, reply
     classification, next-best-action logic and scheduled operational alerts.

2) Founder & Software Engineer - COGEXT (2026-Present)
   AI infrastructure that turns natural-language agent commitments into
   structured, auditable records and measures whether they were fulfilled.
   - FastAPI + PostgreSQL backend for commitment extraction and tracking.
   - LLM extraction pipeline on Groq / Llama 3.3 70B with structured JSON
     output, validation, retries and confidence scoring.
   - 12-state commitment lifecycle state machine with atomic, DB-backed
     transitions and an append-only event history.
   - Evidence ingestion and aggregation using field-level weighted scoring;
     reliability metrics (fulfilment rate, on-time performance, contradiction,
     cancellation, trends).
   - Webhooks with HMAC-SHA256 verification and SSRF protection.
   - PII redaction and privacy controls; human review and confidence calibration.
   - Published Python SDK with 20+ API methods; VitePress docs on Cloudflare.
   - Automated test suite across state transitions, evidence aggregation,
     temporal processing, privacy, webhooks, models, scoring and APIs.

Honest positioning: strong, shipped, production experience, but early-career and
self-taught with no degree and no large-team experience. Best fit is an
early-stage startup, a small product team, or a contract/project engagement that
values someone who can own a product end-to-end.
"""

EMAIL_TEMPLATE = """\
Hi [name/team],

I saw your posting for [role] and it lines up closely with what I build.

I'm a self-taught engineer and solo founder in India shipping production AI
systems end-to-end:
- THRYVIX AI: a live clinic operating system (FastAPI + PostgreSQL + React on
  AWS) with WhatsApp automation and Malayalam voice booking via Sarvam AI.
- COGEXT: LLM extraction over structured JSON with a 12-state lifecycle, plus
  HMAC-signed webhooks, PII redaction and a published Python SDK.

I own architecture through production operations, work async across time zones,
and can start immediately.

Portfolio: thryvixai.com | cogextai.com

Yamin Binyoosuf
yaminbinyoosuf@gmail.com | +91 8304881059
"""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

# Minimal synthetic posting used to probe engine health.
PROBE_JOB = {
    "id": "self-test:1",
    "title": "Backend Engineer (FastAPI / LLM)",
    "company": "Self-test Co",
    "description": "Build FastAPI services and LLM agent workflows in Python.",
    "source": "self-test",
    "tags": ["python", "fastapi", "llm"],
    "location": "Remote (worldwide)",
    "salary": "",
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Addresses that exist in postings but are the wrong destination for an
# application (accommodation requests, legal, abuse desks, placeholders).
_EMAIL_LOCAL_NOISE = (
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "accessib",
    "accommodat",
    "privacy",
    "dpo@",
    "gdpr",
    "legal",
    "compliance",
    "abuse",
    "phishing",
    "security",
    "press",
    "media",
    "support",
    "help",
    "unsubscribe",
    "postmaster",
    "webmaster",
)
_EMAIL_DOMAIN_NOISE = (
    "example.com",
    "example.org",
    "domain.com",
    "yourdomain",
    "sentry.io",
    "wixpress",
    "email.com",
    "test.com",
)


def _clean_html(raw: str | None) -> str:
    """Strip HTML to readable plain text."""
    if not raw:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|h[1-6]|tr)>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _clean_markdown(raw: str | None) -> str:
    """Light markdown strip for sources that publish descriptions as markdown
    (Mercor), so the model sees prose rather than asterisks."""
    if not raw:
        return ""
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", raw)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*[-*]\s+", "- ", text)
    text = re.sub(r"`{1,3}", "", text)
    return text.strip()


def extract_contact_email(text: str | None) -> str | None:
    """Return the first plausible hiring contact address in a posting."""
    if not text:
        return None
    for match in EMAIL_RE.findall(text):
        low = match.lower()
        local, _, domain = low.partition("@")
        if any(noise in local for noise in _EMAIL_LOCAL_NOISE):
            continue
        if any(noise in domain for noise in _EMAIL_DOMAIN_NOISE):
            continue
        if low.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            continue
        return match
    return None


def parse_date(value: Any) -> datetime | None:
    """Best-effort parse of epoch seconds/millis, ISO-8601, or RFC-822.

    Always returns a timezone-aware UTC datetime: boards mix naive ISO strings
    (Freelancer.com) with aware ones, and comparing the two raises TypeError."""
    parsed = _parse_date_raw(value)
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_date_raw(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{9,13}", text):
        return _from_epoch(float(text))
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None


def _from_epoch(value: float) -> datetime | None:
    try:
        if value > 1e11:  # milliseconds
            value /= 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _as_list(value: Any) -> list[str]:
    """Normalize a tag/industry field to a list of strings.

    Several boards (RemoteOK, Arbeitnow, Remotive) serialize their tag arrays
    as Python-style reprs such as "['python', 'llm']", which is not valid JSON,
    so json.loads alone is not enough."""
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            for loader in (json.loads, ast.literal_eval):
                try:
                    parsed = loader(stripped)
                except (ValueError, TypeError, SyntaxError):
                    continue
                if isinstance(parsed, (list, tuple, set)):
                    return [str(v) for v in parsed]
            inner = stripped[1:-1]
            return [part.strip().strip("'\"") for part in inner.split(",") if part.strip()]
        return [stripped]
    return [str(value)]


def _normalize_url(url: str) -> str:
    """Drop scheme/query/fragment/trailing slash so the same posting from two
    boards collapses to one key."""
    if not url:
        return ""
    url = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    url = re.sub(r"^https?://", "", url, flags=re.I)
    return url.lower()


def _title_key(title: str, company: str) -> str:
    def squash(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

    return f"{squash(company)}|{squash(title)}"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})


def _get(url: str, *, as_json: bool = True, retries: int = 3, timeout: int = 30) -> Any:
    """GET with retries and exponential backoff. Raises on final failure."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            resp = _session.get(url, timeout=timeout)
            if resp.status_code >= 500 or resp.status_code == 429:
                raise requests.HTTPError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.json() if as_json else resp.content
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    assert last is not None
    raise last


def _post(url: str, *, headers: dict, payload: dict, retries: int = 3, timeout: int = 90) -> requests.Response:
    """POST with retries on 429/5xx. Returns the final response."""
    last: requests.Response | None = None
    for attempt in range(retries):
        try:
            resp = _session.post(url, headers=headers, json=payload, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            print(f"    request error ({exc}); retrying", file=sys.stderr)
            time.sleep(1.5 * (attempt + 1))
            continue
        last = resp
        # 429 and 5xx are worth retrying; other 4xx (401 bad key, 402 no
        # credit, 400 bad request) are permanent, so fail fast instead of
        # burning the whole run on backoff.
        if resp.status_code != 429 and resp.status_code < 500:
            return resp
        if attempt < retries - 1:
            print(f"    HTTP {resp.status_code}; retrying", file=sys.stderr)
            time.sleep(2.0 * (attempt + 1))
    assert last is not None
    return last


# ---------------------------------------------------------------------------
# Sources — each returns normalized job dicts
# ---------------------------------------------------------------------------


def _work_type_from_types(values: Iterable[str]) -> str:
    """Map a board's own employment-type strings onto our work types."""
    blob = " ".join(str(v).lower() for v in values if v)
    if any(hint in blob for hint in CONTRACT_TYPE_HINTS):
        return "contract"
    return "full_time"


def _job(
    *,
    source: str,
    job_id: str,
    title: str,
    company: str,
    url: str,
    description: str = "",
    tags: Iterable[str] = (),
    location: str = "",
    salary: str = "",
    posted_at: datetime | None = None,
    work_type: str = "full_time",
    no_interview: bool | None = None,
    competition: int | None = None,
) -> dict:
    description = (description or "").strip()
    return {
        "id": f"{source.lower()}:{job_id}",
        "source": source,
        "title": (title or "").strip(),
        "company": (company or "").strip() or "Unknown company",
        "url": (url or "").strip(),
        "description": description,
        "tags": [t for t in tags if t],
        "location": (location or "").strip(),
        "salary": (salary or "").strip(),
        "posted_at": posted_at,
        "contact_email": extract_contact_email(description),
        # "task"     — paid per task/project, claim or bid on it, no interview
        # "contract" — fixed-term contract, usually a light screen at most
        # "full_time" — standard employment, normally a full interview loop
        "work_type": work_type,
        # True/False when the source states it outright; None when unknown.
        "no_interview": no_interview,
        # Competing applicants, where the source exposes it (Freelancer bids).
        "competition": competition,
    }


def fetch_remoteok_jobs() -> list[dict]:
    """RemoteOK's feed; element 0 is a legal notice with no id."""
    data = _get(REMOTEOK_API_URL)
    jobs = []
    for raw in data if isinstance(data, list) else []:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        salary = ""
        if raw.get("salary_min") or raw.get("salary_max"):
            salary = f"${raw.get('salary_min', '?')}-${raw.get('salary_max', '?')}/yr"
        jobs.append(
            _job(
                source="RemoteOK",
                job_id=str(raw["id"]),
                title=raw.get("position", ""),
                company=raw.get("company", ""),
                url=raw.get("url") or raw.get("apply_url") or "",
                description=_clean_html(raw.get("description")),
                tags=_as_list(raw.get("tags")),
                location=raw.get("location", ""),
                salary=salary,
                posted_at=parse_date(raw.get("epoch")) or parse_date(raw.get("date")),
                work_type=_work_type_from_types(_as_list(raw.get("tags"))),
            )
        )
    return jobs


def fetch_wwr_jobs() -> list[dict]:
    """WeWorkRemotely RSS. Titles are 'Company Name: Role Title'."""
    jobs: list[dict] = []
    for feed in WWR_RSS_URLS:
        try:
            content = _get(feed, as_json=False)
            root = ET.fromstring(content)
        except Exception as exc:  # noqa: BLE001
            print(f"  WeWorkRemotely feed failed ({feed}): {exc}", file=sys.stderr)
            continue
        for item in root.findall(".//item"):
            title_raw = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            guid = (item.findtext("guid") or link).strip()
            if not guid:
                continue
            company, sep, position = title_raw.partition(": ")
            if not sep:
                company, position = "", title_raw
            jobs.append(
                _job(
                    source="WeWorkRemotely",
                    job_id=guid,
                    title=position,
                    company=company,
                    url=link,
                    description=_clean_html(item.findtext("description")),
                    posted_at=parse_date(item.findtext("pubDate")),
                )
            )
    return jobs


def fetch_remotive_jobs() -> list[dict]:
    data = _get(REMOTIVE_API_URL)
    jobs = []
    for raw in (data or {}).get("jobs", []):
        jobs.append(
            _job(
                source="Remotive",
                job_id=str(raw.get("id")),
                title=raw.get("title", ""),
                company=raw.get("company_name", ""),
                url=raw.get("url", ""),
                description=_clean_html(raw.get("description")),
                tags=_as_list(raw.get("tags")),
                location=raw.get("candidate_required_location", ""),
                salary=raw.get("salary", ""),
                posted_at=parse_date(raw.get("publication_date")),
                work_type=_work_type_from_types([raw.get("job_type")]),
            )
        )
    return jobs


def fetch_arbeitnow_jobs() -> list[dict]:
    data = _get(ARBEITNOW_API_URL)
    jobs = []
    for raw in (data or {}).get("data", []):
        if str(raw.get("remote", "")).lower() != "true":
            # Arbeitnow is Germany-heavy and mostly on-site; keep only remote.
            continue
        jobs.append(
            _job(
                source="Arbeitnow",
                job_id=raw.get("slug", ""),
                title=raw.get("title", ""),
                company=raw.get("company_name", ""),
                url=raw.get("url", ""),
                description=_clean_html(raw.get("description")),
                tags=_as_list(raw.get("tags")) + _as_list(raw.get("job_types")),
                location=raw.get("location", ""),
                posted_at=parse_date(raw.get("created_at")),
                work_type=_work_type_from_types(_as_list(raw.get("job_types"))),
            )
        )
    return jobs


def fetch_jobicy_jobs() -> list[dict]:
    data = _get(JOBICY_API_URL)
    jobs = []
    for raw in (data or {}).get("jobs", []):
        salary = ""
        if raw.get("salaryMin") or raw.get("salaryMax"):
            salary = (
                f"{raw.get('salaryCurrency', '')} {raw.get('salaryMin', '?')}-"
                f"{raw.get('salaryMax', '?')} {raw.get('salaryPeriod', '')}"
            ).strip()
        jobs.append(
            _job(
                source="Jobicy",
                job_id=str(raw.get("id")),
                title=raw.get("jobTitle", ""),
                company=raw.get("companyName", ""),
                url=raw.get("url", ""),
                description=_clean_html(raw.get("jobDescription") or raw.get("jobExcerpt")),
                tags=_as_list(raw.get("jobIndustry")) + _as_list(raw.get("jobType")),
                location=raw.get("jobGeo", ""),
                salary=salary,
                posted_at=parse_date(raw.get("pubDate")),
                work_type=_work_type_from_types(_as_list(raw.get("jobType"))),
            )
        )
    return jobs


def fetch_himalayas_jobs() -> list[dict]:
    """Himalayas exposes explicit seniority and location restrictions, which
    are exactly the two filters this candidate cares about, so those are
    carried through as tags for the scorer to see."""
    data = _get(HIMALAYAS_API_URL)
    jobs = []
    for raw in (data or {}).get("jobs", []):
        salary = ""
        if raw.get("minSalary") or raw.get("maxSalary"):
            salary = (
                f"{raw.get('currency', '')} {raw.get('minSalary', '?')}-"
                f"{raw.get('maxSalary', '?')} {raw.get('salaryPeriod', '')}"
            ).strip()
        locations = _as_list(raw.get("locationRestrictions")) or _as_list(
            raw.get("timezoneRestrictions")
        )
        jobs.append(
            _job(
                source="Himalayas",
                job_id=str(raw.get("guid") or raw.get("applicationLink") or raw.get("title")),
                title=raw.get("title", ""),
                company=raw.get("companyName", ""),
                url=raw.get("applicationLink") or raw.get("guid") or "",
                description=_clean_html(raw.get("description") or raw.get("excerpt")),
                tags=(
                    _as_list(raw.get("categories"))
                    + _as_list(raw.get("parentCategories"))
                    + _as_list(raw.get("seniority"))
                    + _as_list(raw.get("employmentType"))
                ),
                location=", ".join(locations),
                salary=salary,
                posted_at=parse_date(raw.get("pubDate")),
                work_type=_work_type_from_types(_as_list(raw.get("employmentType"))),
            )
        )
    return jobs


def fetch_workingnomads_jobs() -> list[dict]:
    """Working Nomads exposes a location string such as 'Anywhere in India',
    which is a useful eligibility signal."""
    data = _get(WORKINGNOMADS_API_URL)
    jobs = []
    for raw in data if isinstance(data, list) else []:
        jobs.append(
            _job(
                source="Working Nomads",
                job_id=str(raw.get("url") or raw.get("title")),
                title=raw.get("title", ""),
                company=raw.get("company_name", ""),
                url=raw.get("url", ""),
                description=_clean_html(raw.get("description")),
                tags=[
                    t.strip() for t in str(raw.get("tags") or "").split(",") if t.strip()
                ]
                + _as_list(raw.get("category_name")),
                location=raw.get("location", ""),
                posted_at=parse_date(raw.get("pub_date")),
            )
        )
    return jobs


def fetch_hn_jobs() -> list[dict]:
    """Hacker News 'Ask HN: Who is hiring?' — the newest monthly thread.

    Only top-level comments are job posts. These frequently publish a real
    hiring contact, which is what makes direct outreach possible."""
    listing = _get(HN_SEARCH_URL)
    story = next(
        (
            hit
            for hit in (listing or {}).get("hits", [])
            if (hit.get("title") or "").startswith("Ask HN: Who is hiring?")
        ),
        None,
    )
    if not story:
        return []
    story_id = str(story["objectID"])
    jobs: list[dict] = []
    nb_pages = 1
    for page in range(HN_MAX_PAGES):
        if page >= nb_pages:
            break
        data = _get(
            f"https://hn.algolia.com/api/v1/search"
            f"?tags=comment,story_{story_id}&hitsPerPage=100&page={page}"
        )
        nb_pages = int((data or {}).get("nbPages") or 1)
        hits = (data or {}).get("hits", [])
        if not hits:
            break
        for hit in hits:
            if str(hit.get("parent_id")) != story_id:  # skip replies to comments
                continue
            text = _clean_html(hit.get("comment_text"))
            if len(text) < 40:
                continue
            first_line = text.split("\n", 1)[0][:220]
            company, sep, title = first_line.partition("|")
            if not sep:
                company, title = first_line, first_line
            jobs.append(
                _job(
                    source="HN Who is hiring",
                    job_id=str(hit.get("objectID")),
                    title=title.strip(" |"),
                    company=company.strip(" |"),
                    url=f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                    description=text,
                    posted_at=parse_date(hit.get("created_at")),
                )
            )
        time.sleep(0.3)
    return jobs


# ---------------------------------------------------------------------------
# Task-based / no-interview sources
# ---------------------------------------------------------------------------

_LOCATION_OK_TOKENS = ("india", "asia", "apac", "anywhere", "worldwide", "global", "remote")

# Rough static rates, only used to apply a USD-equivalent freelance floor.
# Deliberately approximate: this is a noise filter, not accounting.
_CURRENCY_TO_USD = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "INR": 0.012,
    "AUD": 0.66,
    "CAD": 0.73,
    "SGD": 0.74,
    "NZD": 0.60,
    "PLN": 0.25,
    "CHF": 1.12,
    "SEK": 0.095,
    "ZAR": 0.054,
    "BRL": 0.18,
    "MXN": 0.050,
    "PHP": 0.017,
    "PKR": 0.0036,
    "BDT": 0.0084,
    "NGN": 0.00065,
}


def location_allows_candidate(eligible: list[str], ineligible: list[str]) -> bool:
    """Whether a posting's stated location restrictions leave room for an
    India-based remote contractor. An empty `eligible` list means no stated
    restriction."""
    if any("india" in item.lower() for item in ineligible):
        return False
    if not eligible:
        return True
    return any(
        any(token in item.lower() for token in _LOCATION_OK_TOKENS) for item in eligible
    )


def fetch_mercor_jobs() -> list[dict]:
    """Mercor — task-based AI-training work, paid hourly per engagement.

    Mercor publishes its board as a server-rendered Next.js payload, so the
    listings are read from __NEXT_DATA__ rather than a private API. The board
    also carries explicit interview metadata, which is what makes it worth
    parsing: `interviewSchedulingEnabled`, `requiredInterviewConfigId` and
    `interviewDuration` together tell us whether a human interview happens."""
    content = _get(MERCOR_JOBS_URL, as_json=False)
    text = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
    if not match:
        raise RuntimeError("Mercor page had no __NEXT_DATA__ payload (layout changed?)")
    data = json.loads(match.group(1))
    queries = (
        ((data.get("props") or {}).get("pageProps") or {}).get("dehydratedState") or {}
    ).get("queries") or []
    listings: list[dict] = []
    for query in queries:
        found = ((query.get("state") or {}).get("data") or {}).get("listings")
        if found:
            listings = found
            break
    if not listings:
        raise RuntimeError("Mercor payload contained no listings")

    jobs: list[dict] = []
    for raw in listings:
        if str(raw.get("status") or "").lower() != "active":
            continue
        if str(raw.get("deletedAt") or "None") not in {"None", "", "none"}:
            continue
        eligible = _as_list(raw.get("eligibleLocation"))
        ineligible = _as_list(raw.get("ineligibleLocation")) + _as_list(
            raw.get("ineligibleResidenceLocation")
        )
        if not location_allows_candidate(eligible, ineligible):
            continue

        # No human interview when scheduling is off and no interview config
        # or duration is attached to the listing.
        interviews_off = (
            str(raw.get("interviewSchedulingEnabled")) == "False"
            and str(raw.get("requiredInterviewConfigId")) == "None"
            and str(raw.get("interviewDuration")) == "None"
        )
        rate = ""
        if raw.get("rateMin") or raw.get("rateMax"):
            frequency = raw.get("payRateFrequency") or "hourly"
            rate = f"${raw.get('rateMin', '?')}-${raw.get('rateMax', '?')}/{frequency}"
        listing_id = str(raw.get("listingId") or raw.get("uid") or "")
        slug = re.sub(r"[^a-z0-9]+", "-", (raw.get("title") or "").lower()).strip("-")
        jobs.append(
            _job(
                source="Mercor",
                job_id=listing_id,
                title=raw.get("title", ""),
                company=raw.get("companyName") or "Mercor client",
                url=f"https://work.mercor.com/jobs/{listing_id}/{slug}",
                description=_clean_markdown(raw.get("description") or ""),
                tags=_as_list(raw.get("listingDomain")) + _as_list(raw.get("commitment")),
                location=", ".join(eligible) or "Remote",
                salary=rate,
                posted_at=parse_date(raw.get("postedAt") or raw.get("createdAt")),
                work_type="task",
                no_interview=interviews_off,
            )
        )
    return jobs


def fetch_freelancer_jobs() -> list[dict]:
    """Freelancer.com — bid-based project work with no interview at all.

    Uses their public projects API. The API's `query` parameter is a fuzzy
    search rather than a filter, so relevance is left to the shared keyword
    prefilter and only budget/competition are filtered here."""
    data = _get(FREELANCER_API_URL)
    projects = ((data or {}).get("result") or {}).get("projects") or []
    jobs: list[dict] = []
    for raw in projects:
        budget = raw.get("budget") or {}
        currency = ((raw.get("currency") or {}).get("code") or "").upper()
        low = float(budget.get("minimum") or 0)
        high = float(budget.get("maximum") or 0)
        # Compare every currency against the floor in USD, otherwise a low INR
        # project sails past a floor meant to filter exactly that.
        rate = _CURRENCY_TO_USD.get(currency)
        if rate is not None and max(low, high) * rate < FREELANCE_MIN_BUDGET:
            continue
        bids = int((raw.get("bid_stats") or {}).get("bid_count") or 0)
        if bids > FREELANCE_MAX_BIDS:
            continue

        amount = ""
        if low or high:
            amount = f"{currency} {low:g}-{high:g}".strip()
            if rate is not None:
                amount += f" (~${max(low, high) * rate:,.0f})"
        if bids:
            amount = f"{amount} · {bids} bids".strip(" ·")

        seo = raw.get("seo_url") or ""
        url = f"https://www.freelancer.com/projects/{seo}" if seo else ""
        jobs.append(
            _job(
                source="Freelancer.com",
                job_id=str(raw.get("id")),
                title=raw.get("title", ""),
                company="Freelancer.com client",
                url=url,
                description=_clean_html(
                    raw.get("description") or raw.get("preview_description")
                ),
                tags=[str(s.get("name")) for s in (raw.get("jobs") or []) if s.get("name")]
                + [str(raw.get("type") or "")],
                location="Remote",
                salary=amount,
                posted_at=parse_date(raw.get("time_submitted") or raw.get("time_updated")),
                work_type="task",
                no_interview=True,
                competition=bids,
            )
        )
    return jobs


def fetch_hn_freelancer_jobs() -> list[dict]:
    """The monthly 'Ask HN: Freelancer? Seeking freelancer?' thread.

    Top-level comments lead with either SEEKING FREELANCER (someone hiring —
    a lead) or SEEKING WORK (another freelancer — a competitor). Only the
    first kind is kept."""
    listing = _get(HN_FREELANCER_SEARCH_URL)
    story = next(
        (
            hit
            for hit in (listing or {}).get("hits", [])
            if (hit.get("title") or "").startswith("Ask HN: Freelancer?")
        ),
        None,
    )
    if not story:
        return []
    story_id = str(story["objectID"])
    jobs: list[dict] = []
    nb_pages = 1
    for page in range(HN_MAX_PAGES):
        if page >= nb_pages:
            break
        data = _get(
            f"https://hn.algolia.com/api/v1/search"
            f"?tags=comment,story_{story_id}&hitsPerPage=100&page={page}"
        )
        nb_pages = int((data or {}).get("nbPages") or 1)
        hits = (data or {}).get("hits", [])
        if not hits:
            break
        for hit in hits:
            if str(hit.get("parent_id")) != story_id:
                continue
            text = _clean_html(hit.get("comment_text"))
            upper = text.upper()
            if "SEEKING FREELANCER" not in upper:
                continue  # SEEKING WORK posts are competitors, not leads
            first_line = text.split("\n", 1)[0][:220]
            company, sep, title = first_line.partition("|")
            if not sep:
                company, title = "HN client", first_line
            jobs.append(
                _job(
                    source="HN Freelancer",
                    job_id=str(hit.get("objectID")),
                    title=title.strip(" |") or "Freelance engagement",
                    company=company.strip(" |"),
                    url=f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                    description=text,
                    posted_at=parse_date(hit.get("created_at")),
                    work_type="task",
                    no_interview=True,
                )
            )
        time.sleep(0.3)
    return jobs


FETCHERS: list[tuple[str, Any]] = [
    # Task-based / no-interview work first — this is what gets prioritised.
    ("Mercor", fetch_mercor_jobs),
    ("Freelancer.com", fetch_freelancer_jobs),
    ("HN Freelancer", fetch_hn_freelancer_jobs),
    ("RemoteOK", fetch_remoteok_jobs),
    ("WeWorkRemotely", fetch_wwr_jobs),
    ("Remotive", fetch_remotive_jobs),
    ("Arbeitnow", fetch_arbeitnow_jobs),
    ("Jobicy", fetch_jobicy_jobs),
    ("Himalayas", fetch_himalayas_jobs),
    ("Working Nomads", fetch_workingnomads_jobs),
    ("HN Who is hiring", fetch_hn_jobs),
]


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def title_is_blocked(job: dict) -> bool:
    title = f" {job.get('title', '').lower()} "
    if any(bad in title for bad in TITLE_BLOCKLIST):
        return True
    return bool(GERMAN_TITLE_RE.search(title))


def keyword_evidence(job: dict) -> tuple[int, int]:
    """Return (strong, weak) keyword evidence.

    Strong means the keyword appears in the title or the board's own tags,
    which is what actually describes the role. Weak means it only appears
    somewhere in the body text, which is usually incidental — a marketing
    post mentioning "automation" is not an engineering job."""
    title_tags = " ".join(
        [job.get("title", ""), " ".join(job.get("tags", []) or [])]
    ).lower()
    description = (job.get("description") or "").lower()
    strong = sum(1 for kw in KEYWORDS if kw in title_tags)
    weak = sum(
        1 for kw in KEYWORDS if kw not in title_tags and kw in description
    )
    return strong, weak


def keyword_hits(job: dict) -> int:
    strong, weak = keyword_evidence(job)
    return strong * 2 + weak


def matches_keywords(job: dict) -> bool:
    strong, weak = keyword_evidence(job)
    return strong >= 1 or weak >= 3


def dedupe(jobs: list[dict]) -> list[dict]:
    """Collapse the same posting syndicated across multiple boards."""
    out: list[dict] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    for job in jobs:
        url_key = _normalize_url(job.get("url", ""))
        title_key = _title_key(job.get("title", ""), job.get("company", ""))
        if url_key and url_key in seen_urls:
            continue
        if title_key in seen_titles:
            continue
        if url_key:
            seen_urls.add(url_key)
        seen_titles.add(title_key)
        out.append(job)
    return out


def select_candidates(jobs: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc)
    # Task and contract boards are standing catalogs and get a much longer
    # window than time-sensitive full-time job postings.
    cutoff_fresh = now - timedelta(hours=LOOKBACK_HOURS)
    cutoff_standing = now - timedelta(hours=LOOKBACK_HOURS_TASK)
    picked = []
    for job in jobs:
        work_type = job.get("work_type", "full_time")
        cutoff = cutoff_standing if work_type in ("task", "contract") else cutoff_fresh
        posted_at = job.get("posted_at")
        if posted_at is not None:
            if posted_at.tzinfo is None:
                posted_at = posted_at.replace(tzinfo=timezone.utc)
            if posted_at < cutoff:
                continue
        # The source stated outright that this one runs interviews: not wanted.
        if job.get("no_interview") is False:
            continue
        if title_is_blocked(job):
            continue
        if not matches_keywords(job):
            continue
        picked.append(job)

    # Task work first, then contract, then full-time. Within a work type: best
    # keyword overlap, then least contested, then freshest — so a capped run
    # spends its API budget on the work that is actually wanted.
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)

    def sort_key(job: dict):
        posted = job.get("posted_at") or epoch
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return (
            WORK_TYPE_RANK.get(job.get("work_type", "full_time"), 2),
            -keyword_hits(job),
            job.get("competition") or 0,
            -posted.timestamp(),
        )

    return sorted(picked, key=sort_key)


# ---------------------------------------------------------------------------
# DeepSeek scoring + outreach drafting
# ---------------------------------------------------------------------------


def _build_score_prompt(job: dict) -> str:
    description = (job.get("description") or "")[:6000]
    return f"""You are screening remote job postings for a specific candidate and drafting their outreach.

CANDIDATE PROFILE:
{PROFILE}

OUTREACH EMAIL TEMPLATE (match its tone and structure, adapt the content):
{EMAIL_TEMPLATE}

JOB POSTING:
Source: {job.get('source', '')}
Title: {job.get('title', '')}
Company: {job.get('company', '')}
Location: {job.get('location', '')}
Rate/budget: {job.get('salary', '')}
Tags: {', '.join(job.get('tags', []) or [])}
Work type: {job.get('work_type', 'full_time')}
Description:
{description}

HARD REQUIREMENT FROM THE CANDIDATE: they will NOT sit job interviews. They
want task-based or project-based work they can simply start, or contract work
with no interview loop. A conventional full-time role is acceptable ONLY if
the posting genuinely indicates no interview (for example "no interview",
"no whiteboard", "paid trial task", "take-home only", or "start immediately").
A normal multiple-round interview process is useless to them however good the
stack match is.

Score this posting 1-10 on "should this candidate take this on", using this scale:
  9-10  Task/project work they can start now, squarely within their stack.
  7-8   Task or contract work with strong stack overlap, or a no-interview
        full-time role that fits well.
  5-6   Plausible, with a real gap (adjacent stack, modest budget, or a
        "worth asking" location caveat).
  1-4   Not worth their time.

Judge whether this candidate could actually win and do this work, NOT whether
they satisfy every listed requirement. Postings list aspirational wishlists:
treat "3+ years", "bonus points" and long technology lists as soft unless the
posting stresses them. Do NOT penalise them for having no degree, for being
self-taught, or for working solo — those are strengths here.

Cap the score at 4 if it is a conventional full-time role with a normal
interview process (multiple rounds, live coding, or a panel).
Cap the score at 3 if it requires residency or work authorisation the
candidate cannot have, or fluency in a language they do not speak.
Cap the score at 5 if it demands 5+ years, or is staff/principal/director level.

Then draft the outreach. For task/project work this is a short proposal to win
the work; for a job it is a short application email. Under 150 words, following
the template's tone. Replace the [name/team] and [role] placeholders, and tie
ONE or TWO of the candidate's specific shipped projects (THRYVIX AI clinic OS,
COGEXT commitment infrastructure) to what the posting actually needs. Never
invent experience the profile does not support. Sign off as Yamin Binyoosuf.

Respond with ONLY a JSON object, no prose, in exactly this shape:
{{"score": <integer 1-10>,
  "reason": "<one or two sentences explaining the score, naming the decisive factor>",
  "interview_process": "<one of: none | light | standard>",
  "work_type": "<one of: task | contract | full_time>",
  "email_subject": "<subject line under 80 characters>",
  "email_body": "<the outreach email, plain text, with real newlines>"}}"""


def _parse_json_response(raw: str) -> dict:
    """Parse a JSON object out of model output, tolerating code fences."""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object in model output: {text[:200]}")
    return json.loads(match.group(0))


def check_deepseek_balance() -> tuple[bool, str]:
    """Ask DeepSeek whether this key can actually spend money.

    Uses DeepSeek's free /user/balance endpoint. This turns an opaque
    'Insufficient Balance' 402 in the middle of a run into a clear
    up-front message the user can act on."""
    if not DEEPSEEK_API_KEY:
        return False, "DEEPSEEK_API_KEY is not set"
    url = f"{DEEPSEEK_BASE_URL.rstrip('/')}/user/balance"
    try:
        resp = _session.get(
            url, headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"}, timeout=20
        )
    except Exception as exc:  # noqa: BLE001
        return True, f"balance check unreachable ({type(exc).__name__}); proceeding anyway"

    if resp.status_code == 401:
        return False, "API key rejected (HTTP 401) — check DEEPSEEK_API_KEY"
    if resp.status_code >= 400:
        return True, f"balance check returned HTTP {resp.status_code}; proceeding anyway"
    try:
        data = resp.json()
    except ValueError:
        return True, "balance response unreadable; proceeding anyway"

    infos = data.get("balance_infos") or []
    detail = ", ".join(
        f"{i.get('currency', '')} {i.get('total_balance', '?')}".strip() for i in infos
    )
    if data.get("is_available") is False:
        return False, f"account has no credit ({detail or 'balance is zero'})"
    return True, detail or "available"


def _deepseek_json(prompt: str, model: str) -> dict:
    """One DeepSeek JSON-mode call, returning the parsed object."""
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")
    resp = _post(
        f"{DEEPSEEK_BASE_URL.rstrip('/')}/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        payload={
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a precise technical recruiter. You always reply with a "
                        "single valid JSON object and nothing else."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": DEEPSEEK_MAX_TOKENS,
            "response_format": {"type": "json_object"},
        },
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"DeepSeek HTTP {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    choice = payload["choices"][0]
    message = choice.get("message") or {}
    raw = (message.get("content") or "").strip()
    if not raw:
        # deepseek-v4-pro and deepseek-flash are reasoning models: they spend
        # the token budget on reasoning_content first, so an exhausted budget
        # surfaces as an empty content field instead of an HTTP error.
        reasoning = message.get("reasoning_content") or ""
        raise RuntimeError(
            f"DeepSeek returned empty content (finish_reason={choice.get('finish_reason')}, "
            f"reasoning_chars={len(reasoning)}, model={model}). "
            f"Raise DEEPSEEK_MAX_TOKENS (currently {DEEPSEEK_MAX_TOKENS}) or use a "
            "non-reasoning model such as deepseek-chat."
        )
    return _parse_json_response(raw)


def score_and_draft_deepseek(job: dict) -> dict:
    """Score a posting and draft a first outreach with the bulk model."""
    result = _deepseek_json(_build_score_prompt(job), DEEPSEEK_MODEL)
    try:
        result["score"] = max(1, min(10, int(result.get("score", 0))))
    except (TypeError, ValueError):
        result["score"] = 0
    return result


def _build_draft_prompt(job: dict, score: int, reason: str) -> str:
    """Focused prompt for the strong model: write the email only.

    Re-asking for the score would waste reasoning tokens on work the bulk
    model already did, so this is deliberately narrow."""
    return f"""Write the final outreach email for a specific candidate.

CANDIDATE PROFILE:
{PROFILE}

OUTREACH EMAIL TEMPLATE (follow its tone and structure):
{EMAIL_TEMPLATE}

JOB POSTING:
Title: {job.get('title', '')}
Company: {job.get('company', '')}
Location: {job.get('location', '')}
Description:
{(job.get('description') or '')[:4000]}

An initial screen rated this posting {score}/10 with the note: {reason}

Write the single best outreach email for this candidate. Replace the [name/team]
and [role] placeholders, and tie ONE or TWO of the candidate's shipped projects
(THRYVIX AI clinic OS, COGEXT commitment infrastructure) to what this posting
actually needs. Under 150 words, plain text, no markdown. Never invent
experience the profile does not support. Sign off as Yamin Binyoosuf.

Respond with ONLY a JSON object:
{{"email_subject": "<subject line under 80 characters>",
  "email_body": "<the email, plain text, with real newlines>"}}"""


def draft_email_deepseek(job: dict, score: int, reason: str) -> dict:
    """Draft the outreach with the stronger drafting model."""
    return _deepseek_json(_build_draft_prompt(job, score, reason), DEEPSEEK_DRAFT_MODEL)


def score_and_draft_gemini(
    client: "_genai.Client", model: str, job: dict, attempts: int = 3
) -> dict:
    """Gemini fallback. Retries transient 503/429 overload responses, which
    the free tier returns often."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            response = client.models.generate_content(
                model=model, contents=_build_score_prompt(job)
            )
            result = _parse_json_response(response.text or "")
            try:
                result["score"] = max(1, min(10, int(result.get("score", 0))))
            except (TypeError, ValueError):
                result["score"] = 0
            return result
        except Exception as exc:  # noqa: BLE001
            last = exc
            text = str(exc)
            transient = any(
                marker in text
                for marker in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "overloaded")
            )
            if not transient or attempt == attempts - 1:
                raise
            print(f"    Gemini transient error; backing off ({text[:80]})", file=sys.stderr)
            time.sleep(3.0 * (attempt + 1))
    assert last is not None
    raise last


def select_working_gemini_model(client: "_genai.Client", probe_job: dict) -> tuple[str | None, list[str]]:
    """Probe Gemini models once at startup and return the first one that
    actually responds, plus the full ordered candidate list for mid-run
    rotation. Free-tier 503s are model-specific, so the model named in
    GEMINI_MODEL_PREFERENCES may be unavailable while another works."""
    visible: set[str] = set()
    try:
        for model in client.models.list():
            visible.add((model.name or "").removeprefix("models/"))
    except Exception as exc:  # noqa: BLE001
        print(f"  Gemini model listing failed ({exc})", file=sys.stderr)

    candidates = [m for m in GEMINI_MODEL_PREFERENCES if not visible or m in visible] or list(
        GEMINI_MODEL_PREFERENCES
    )
    if GEMINI_MODEL_ENV:
        candidates = [GEMINI_MODEL_ENV] + [c for c in candidates if c != GEMINI_MODEL_ENV]

    for candidate in candidates:
        try:
            score_and_draft_gemini(client, candidate, probe_job, attempts=1)
        except Exception as exc:  # noqa: BLE001
            print(f"    Gemini {candidate}: {str(exc)[:90]}", file=sys.stderr)
            continue
        return candidate, [candidate] + [c for c in candidates if c != candidate]
    return None, candidates


def score_and_draft_gemini_multi(client: "_genai.Client", models: list[str], job: dict) -> dict:
    """Try each candidate model until one answers."""
    last: Exception | None = None
    for index, model in enumerate(models):
        try:
            return score_and_draft_gemini(client, model, job, attempts=1 if index else 2)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if index < len(models) - 1:
                print(f"    {model} unavailable; trying next model", file=sys.stderr)
    assert last is not None
    raise last


# ---------------------------------------------------------------------------
# Resend
# ---------------------------------------------------------------------------


def send_email_via_resend(
    api_key: str,
    subject: str,
    body: str,
    to: str | None = None,
    reply_to: str | None = None,
) -> bool:
    if DRY_RUN:
        print(f"  [DRY_RUN] would email {to or NOTIFY_EMAIL}: {subject}")
        return False
    recipient = to or NOTIFY_EMAIL
    payload: dict[str, Any] = {
        "from": RESEND_FROM,
        "to": [recipient],
        "subject": subject,
        "text": body,
    }
    if reply_to:
        payload["reply_to"] = reply_to
    resp = _post(
        RESEND_API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        payload=payload,
        retries=2,
        timeout=30,
    )
    if resp.status_code >= 400:
        print(f"  Resend error {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def migrate_log_if_needed() -> None:
    """Repair a jobs_log.csv written by an older schema.

    The historical file used `source` + `notified`; the code writes `emailed`.
    Reading it with DictReader silently reported every row as not-emailed, so
    strong matches were re-scored and re-emailed on every run."""
    if not LOG_PATH.exists():
        return
    with LOG_PATH.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames or []
        rows = list(reader)
    if header == LOG_FIELDS:
        return

    backup = LOG_PATH.with_suffix(".csv.bak")
    try:
        shutil.copy2(LOG_PATH, backup)
    except OSError as exc:  # noqa: BLE001
        print(f"  Could not back up jobs_log.csv ({exc}); continuing", file=sys.stderr)

    print(f"  Migrating jobs_log.csv header {header} -> {LOG_FIELDS}")
    repaired: list[dict] = []
    for row in rows:
        normalized = {field: row.get(field, "") or "" for field in LOG_FIELDS}
        # Legacy column names.
        if not normalized["emailed"]:
            normalized["emailed"] = row.get("notified", "") or ""
        if not normalized.get("source"):
            normalized["source"] = row.get("source", "") or ""
        if not normalized["job_id"]:
            continue
        repaired.append(normalized)

    with LOG_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOG_FIELDS)
        writer.writeheader()
        writer.writerows(repaired)
    print(f"  jobs_log.csv repaired: {len(repaired)} rows")


def _row_emailed(row: dict) -> bool:
    return str(row.get("emailed", "")).strip().lower() in {"true", "1", "yes"}


def load_seen_job_ids() -> set[str]:
    """Job ids we have finished with.

    A row counts as done only when a final decision was recorded against the
    current rubric. A scoring error, a strong match whose email never went out,
    or a below-threshold score from an older rubric must all be revisited."""
    if not LOG_PATH.exists():
        return set()
    seen: set[str] = set()
    for row in _read_log_rows():
        job_id = (row.get("job_id") or "").strip()
        if not job_id:
            continue
        if (row.get("reason") or "").startswith("error:"):
            continue
        emailed = _row_emailed(row)
        try:
            score = int(float(row.get("score") or 0))
        except ValueError:
            score = 0
        # Never re-send something already sent.
        if emailed:
            seen.add(job_id)
            continue
        # A match whose email failed must be retried.
        if score >= SCORE_THRESHOLD:
            continue
        # Below threshold, but judged by an older rubric: re-evaluate once.
        if (row.get("rubric") or "") != RUBRIC_VERSION:
            continue
        seen.add(job_id)
    return seen


def _read_log_rows() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    with LOG_PATH.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def append_log(rows: list[dict]) -> None:
    if not rows:
        return
    is_new = not LOG_PATH.exists()
    with LOG_PATH.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOG_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in LOG_FIELDS} for row in rows)


# ---------------------------------------------------------------------------
# COGEXT commitment tracking
# ---------------------------------------------------------------------------


def track_commitment(job: dict, score: int) -> None:
    if not COGEXT_API_KEY or DRY_RUN:
        return
    session_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, job["id"]))
    message = (
        f"I will apply to the {job.get('title')} role at {job.get('company')} "
        f"(score {score}/10). I will send the outreach email and follow up within "
        f"48 hours. Apply URL: {job.get('url')}"
    )
    try:
        resp = _post(
            f"{COGEXT_API_URL}/ingest",
            headers={
                "Authorization": f"Bearer {COGEXT_API_KEY}",
                "Content-Type": "application/json",
            },
            payload={
                "source_agent_id": COGEXT_AGENT_ID,
                "session_id": session_id,
                "message": message,
            },
            retries=1,
            timeout=15,
        )
        if resp.status_code == 200:
            print("    COGEXT: commitment tracked")
        else:
            print(f"    COGEXT: ingest returned {resp.status_code}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"    COGEXT: tracking failed ({exc})", file=sys.stderr)


# ---------------------------------------------------------------------------
# Email composition
# ---------------------------------------------------------------------------


def build_match_email(job: dict, result: dict) -> tuple[str, str]:
    score = result.get("score")
    subject = result.get("email_subject") or f"AI/Python engineer available — {job.get('title')}"
    contact = job.get("contact_email")
    lines = [
        f"Match score: {score}/10",
        f"Work type:   {(result.get('work_type') or job.get('work_type') or '').upper()}"
        f"  |  interview: {result.get('interview_process') or 'n/a'}",
        f"Role:        {job.get('title')}",
        f"Company:     {job.get('company')}",
        f"Source:      {job.get('source')}",
        f"Posted:      {job['posted_at'].strftime('%Y-%m-%d %H:%M UTC') if job.get('posted_at') else 'unknown'}",
    ]
    if job.get("location"):
        lines.append(f"Location:    {job['location']}")
    if job.get("salary"):
        lines.append(f"Rate/budget: {job['salary']}")
    if job.get("competition"):
        lines.append(f"Competition: {job['competition']} other applicants")
    lines += [
        f"Apply:       {job.get('url')}",
    ]
    if contact:
        lines.append(f"Contact:     {contact}  <-- published in the posting")
    lines += [
        "",
        f"Why it scored {score}: {result.get('reason', '')}",
        "",
        "=" * 68,
        "READY-TO-SEND OUTREACH (copy, paste, send)",
        "=" * 68,
        "",
        f"Subject: {subject}",
        "",
        result.get("email_body", "").strip(),
        "",
        "=" * 68,
    ]
    if job.get("description"):
        snippet = job["description"][:1600]
        lines += ["POSTING EXCERPT", "-" * 68, snippet, ""]
    headline = f"[{score}/10] {job.get('title')} @ {job.get('company')}"
    if (job.get("work_type") or "full_time") == "task":
        headline = f"[TASK] {headline}"
    return headline, "\n".join(lines)


def error_row(job: dict, exc: Exception) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "job_id": job["id"],
        "source": job.get("source", ""),
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "url": job.get("url", ""),
        "score": "",
        "emailed": False,
        "reason": f"error: {type(exc).__name__}: {exc}"[:400],
        "rubric": "",
    }


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def self_test(resend_key: str) -> int:
    """Verify the DeepSeek and Resend wiring without touching the job feeds."""
    failures = 0
    probe = PROBE_JOB
    print("== Self-test ==")
    print(f"  DEEPSEEK_API_KEY: {'set' if DEEPSEEK_API_KEY else 'MISSING'}")
    print(f"  RESEND_API_KEY:   {'set' if resend_key else 'MISSING'}")
    print(f"  GEMINI_API_KEY:   {'set' if os.environ.get('GEMINI_API_KEY') else 'not set'}")
    print(f"  DEEPSEEK_MODEL:   {DEEPSEEK_MODEL}")
    print(f"  DRAFT_MODEL:      {DEEPSEEK_DRAFT_MODEL or '(disabled — bulk draft only)'}")
    print(f"  RESEND_FROM:      {RESEND_FROM}")
    print(f"  NOTIFY_EMAIL:     {NOTIFY_EMAIL}")
    print(f"  AUTO_APPLY:       {AUTO_APPLY} (min score {AUTO_APPLY_MIN_SCORE})")
    print(f"  DRY_RUN:          {DRY_RUN}")
    print(
        f"  NO_INTERVIEW_ONLY:{NO_INTERVIEW_ONLY}  "
        f"(task/contract always pass; full-time needs none/light interview)"
    )
    print(
        f"  Freelance floor:  min ${FREELANCE_MIN_BUDGET:g} USD budget, "
        f"max {FREELANCE_MAX_BIDS} rival bids"
    )

    if DEEPSEEK_API_KEY:
        usable, status = check_deepseek_balance()
        print(f"  DeepSeek balance: {status}")
        try:
            started = time.monotonic()
            result = score_and_draft_deepseek(probe)
            elapsed = time.monotonic() - started
            print(f"  DeepSeek call OK in {elapsed:.1f}s — score={result.get('score')}")
            print(f"    reason:  {str(result.get('reason'))[:160]}")
            print(f"    subject: {str(result.get('email_subject'))[:120]}")
            if not result.get("email_body"):
                failures += 1
                print("    but the draft body was empty", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            if not usable:
                print(
                    "  DeepSeek call skipped — the account has no credit. "
                    "Top up at platform.deepseek.com to enable it.",
                    file=sys.stderr,
                )
            else:
                failures += 1
                print(f"  DeepSeek call FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
    else:
        failures += 1
        print("  DeepSeek not configured; skipping call.", file=sys.stderr)

    # The Gemini path is only a fallback, so a failure here is a warning.
    gemini_key = os.environ.get("GEMINI_API_KEY") or ""
    if gemini_key and _genai is not None:
        try:
            client = _genai.Client(api_key=gemini_key)
            model, models = select_working_gemini_model(client, probe)
            if model:
                result = score_and_draft_gemini(client, model, probe)
                print(f"  Gemini fallback OK — model={model}, score={result.get('score')}")
            else:
                print(
                    f"  No Gemini model responded out of {len(models)} candidates "
                    "(non-fatal; DeepSeek is the primary engine).",
                    file=sys.stderr,
                )
        except Exception as exc:  # noqa: BLE001
            print(f"  Gemini fallback failed (non-fatal): {type(exc).__name__}: {exc}", file=sys.stderr)

    if resend_key:
        ok = send_email_via_resend(
            resend_key,
            "job-agent self-test: email delivery works",
            "If you are reading this, Resend delivery from the job agent works.\n",
        )
        if not ok and not DRY_RUN:
            failures += 1
            print("  Resend send FAILED (see error above).", file=sys.stderr)
        elif ok:
            print("  Resend send OK.")
    else:
        failures += 1
        print("  Resend not configured; skipping send.", file=sys.stderr)

    # Whether direct outreach can actually go out. Probing this against a real
    # external address would mean emailing a stranger, and a resend.dev
    # recipient proves nothing (it is Resend's own domain). So report the
    # configuration condition that genuinely determines it.
    if AUTO_APPLY:
        ready, why = outreach_sender_ready()
        if ready:
            print(
                f"  Direct outreach: READY — sending as {RESEND_FROM}, "
                f"replies to {OUTREACH_REPLY_TO}"
            )
        else:
            print(f"  Direct outreach: DISABLED — {why}", file=sys.stderr)
    else:
        print("  Direct outreach: off (AUTO_APPLY not enabled)")

    print(f"== Self-test complete: {'FAILURES: ' + str(failures) if failures else 'all good'} ==")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    args = set(sys.argv[1:])
    global DRY_RUN
    if "--dry-run" in args:
        DRY_RUN = True

    resend_key = os.environ.get("RESEND_API_KEY") or ""

    if "--self-test" in args:
        return self_test(resend_key)

    if not resend_key:
        print("RESEND_API_KEY is not set", file=sys.stderr)
        return 1

    # Pick the engine: DeepSeek preferred, Gemini fallback.
    use_deepseek = bool(DEEPSEEK_API_KEY)
    gemini_client = None
    gemini_model = None
    gemini_models: list[str] = []

    def init_gemini() -> bool:
        nonlocal gemini_client, gemini_model, gemini_models
        gemini_key = os.environ.get("GEMINI_API_KEY")
        if not gemini_key:
            print("  GEMINI_API_KEY is not set", file=sys.stderr)
            return False
        if _genai is None:
            print(
                "  google-genai is not installed; run: pip install google-genai",
                file=sys.stderr,
            )
            return False
        gemini_client = _genai.Client(api_key=gemini_key)
        gemini_model, gemini_models = select_working_gemini_model(gemini_client, PROBE_JOB)
        if not gemini_model:
            print("  No Gemini model responded to a probe request", file=sys.stderr)
            return False
        print(f"Engine: Gemini fallback ({gemini_model})")
        return True

    if use_deepseek:
        usable, status = check_deepseek_balance()
        if usable:
            print(f"Engine: DeepSeek ({DEEPSEEK_MODEL}) — {status}")
        else:
            print(f"DeepSeek is not usable: {status}", file=sys.stderr)
            print("Falling back to Gemini for this run.", file=sys.stderr)
            use_deepseek = False

    if not use_deepseek and not init_gemini():
        print(
            "No usable scoring engine. Top up DeepSeek (platform.deepseek.com) "
            "or set GEMINI_API_KEY.",
            file=sys.stderr,
        )
        return 1

    direct_send_ok = False
    if AUTO_APPLY:
        direct_send_ok, why = outreach_sender_ready()
        if direct_send_ok:
            print(
                f"AUTO_APPLY is ON — direct outreach for scores >= "
                f"{AUTO_APPLY_MIN_SCORE}; replies go to {OUTREACH_REPLY_TO}"
            )
        else:
            print(f"AUTO_APPLY is ON, but direct sending is disabled: {why}", file=sys.stderr)
            print("  Every match and draft still arrives in your inbox.", file=sys.stderr)

    migrate_log_if_needed()

    print("Fetching listings...")
    all_jobs: list[dict] = []
    for name, fetcher in FETCHERS:
        try:
            jobs = fetcher()
            print(f"  {name}: {len(jobs)} listings")
            all_jobs.extend(jobs)
        except Exception as exc:  # noqa: BLE001 - one dead source must not kill the run
            print(f"  {name}: fetch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"  {len(all_jobs)} total listings")

    all_jobs = dedupe(all_jobs)
    print(f"  {len(all_jobs)} unique listings after de-duplication")

    candidates = select_candidates(all_jobs)
    print(f"  {len(candidates)} match keywords and are inside {LOOKBACK_HOURS}h")

    seen_ids = load_seen_job_ids()
    fresh = [job for job in candidates if job["id"] not in seen_ids]
    print(f"  {len(fresh)} not yet processed")

    queue = fresh[:MAX_JOBS_PER_RUN]
    if len(fresh) > len(queue):
        print(f"  capping this run at {MAX_JOBS_PER_RUN} postings ({len(fresh) - len(queue)} deferred)")

    log_rows: list[dict] = []
    emailed_count = 0
    auto_applied = 0
    last_gemini_call = 0.0

    for job in queue:
        title = job.get("title") or "Unknown role"
        company = job.get("company") or "Unknown company"

        if not use_deepseek:
            wait = GEMINI_MIN_INTERVAL_S - (time.monotonic() - last_gemini_call)
            if wait > 0:
                time.sleep(wait)
            last_gemini_call = time.monotonic()

        try:
            if use_deepseek:
                result = score_and_draft_deepseek(job)
            else:
                result = score_and_draft_gemini_multi(gemini_client, gemini_models, job)
        except Exception as exc:  # noqa: BLE001 - log and continue on any API hiccup
            print(f"  [{title} @ {company}] scoring failed: {exc}", file=sys.stderr)
            log_rows.append(error_row(job, exc))
            continue

        score = result.get("score", 0)
        reason = str(result.get("reason", ""))[:400]
        print(f"  [{score}/10] {title} @ {company} — {reason[:100]}")

        # The candidate will not sit interviews. Task and contract work passes
        # by its nature; a full-time posting has to show no interview in its
        # own text before it is worth sending.
        model_work_type = str(result.get("work_type") or "").strip().lower()
        work_type = job.get("work_type") or "full_time"
        if model_work_type in WORK_TYPE_RANK and work_type == "full_time":
            work_type = model_work_type
        interview_process = str(result.get("interview_process") or "").strip().lower()
        no_interview_ok = (
            not NO_INTERVIEW_ONLY
            or work_type in ("task", "contract")
            or interview_process in ("none", "light")
        )

        emailed = False
        if score >= SCORE_THRESHOLD and no_interview_ok:
            # Second pass: let the stronger model rewrite the outreach. Only
            # runs for matches, so the expensive model is used sparingly.
            if (
                use_deepseek
                and DEEPSEEK_DRAFT_MODEL
                and DEEPSEEK_DRAFT_MODEL != DEEPSEEK_MODEL
            ):
                try:
                    better = draft_email_deepseek(job, score, reason)
                    if (better.get("email_body") or "").strip():
                        result["email_body"] = better["email_body"]
                        if better.get("email_subject"):
                            result["email_subject"] = better["email_subject"]
                        print(f"    outreach rewritten by {DEEPSEEK_DRAFT_MODEL}")
                except Exception as exc:  # noqa: BLE001 - keep the first draft
                    print(
                        f"    {DEEPSEEK_DRAFT_MODEL} drafting failed "
                        f"({type(exc).__name__}: {str(exc)[:90]}); keeping the "
                        f"{DEEPSEEK_MODEL} draft",
                        file=sys.stderr,
                    )

            track_commitment(job, score)
            subject, body = build_match_email(job, result)
            emailed = send_email_via_resend(resend_key, subject, body)
            if emailed:
                emailed_count += 1

            # Optionally apply directly, but only to an address the posting
            # itself published, and only for genuinely strong matches.
            contact = job.get("contact_email")
            if AUTO_APPLY and direct_send_ok and contact and emailed and score >= AUTO_APPLY_MIN_SCORE:
                direct_subject = result.get("email_subject") or subject
                direct_body = (result.get("email_body") or "").strip()
                if direct_body:
                    if send_email_via_resend(
                        resend_key,
                        direct_subject,
                        direct_body,
                        to=contact,
                        reply_to=OUTREACH_REPLY_TO,
                    ):
                        auto_applied += 1
                        print(f"    direct outreach sent to {contact}")
            elif AUTO_APPLY and direct_send_ok and contact and emailed:
                print(
                    f"    skipped direct send to {contact}: score {score} is below "
                    f"AUTO_APPLY_MIN_SCORE={AUTO_APPLY_MIN_SCORE}"
                )
        elif score >= SCORE_THRESHOLD:
            print(
                f"    skipped: scored {score} but it needs "
                f"{interview_process or 'a standard'} interview — you asked for none"
            )

        log_rows.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "job_id": job["id"],
                "source": job.get("source", ""),
                "title": title,
                "company": company,
                "url": job.get("url", ""),
                "score": score,
                "emailed": emailed,
                "reason": reason,
                "rubric": RUBRIC_VERSION,
            }
        )

    append_log(log_rows)
    print(f"Done. {len(log_rows)} postings processed, {emailed_count} match email(s) to {NOTIFY_EMAIL}.")
    if AUTO_APPLY:
        print(f"Direct outreach sent: {auto_applied}")
    if DRY_RUN:
        print("DRY_RUN was on — no email was actually sent.")
    print(f"Full log: {LOG_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
