#!/usr/bin/env python3
"""
Automated job search agent.

Searches RemoteOK for jobs matching Yamin's skill set, filters to postings
from the last 24 hours, scores each one with Gemini, and — for anything
scoring 7+ — emails a ready-to-send outreach draft via Resend.

RemoteOK listings don't expose a hiring-manager email address (they link to
an apply page), so this agent can't cold-email companies directly. Instead
it sends the drafted outreach + job link to NOTIFY_EMAIL so Yamin can review
and paste it into the actual application form/portal within minutes of the
posting going live.

Required environment variables:
    GEMINI_API_KEY      Google Gemini API key
    RESEND_API_KEY      Resend API key

Optional environment variables:
    RESEND_FROM         Verified sender address (default: onboarding@resend.dev,
                         Resend's shared test sender — see README)
    NOTIFY_EMAIL        Where match digests are sent (default: yaminbinyoosuf@gmail.com)

Optional:
    COGEXT_API_KEY      COGEXT API key for commitment tracking (cogextai.com)
"""

import csv
import uuid
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

# google-genai is only imported when GEMINI_API_KEY is used (not DeepSeek).
try:
    from google import genai as _genai  # type: ignore
except ImportError:  # pragma: no cover
    _genai = None  # type: ignore

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REMOTEOK_API_URL = "https://remoteok.com/api"
WWR_RSS_URL = "https://weworkremotely.com/categories/remote-programming-jobs.rss"
KEYWORDS = [
    "ai agent",
    "agentic",
    "fastapi",
    "whatsapp",
    "voice agent",
    "llm",
    "python",
    "backend",
    "automation",
]
SCORE_THRESHOLD = 7
LOOKBACK_HOURS = 72

# DeepSeek (preferred when DEEPSEEK_API_KEY is set — no extra package, uses requests).
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY") or ""
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL") or "deepseek-chat"

# Gemini fallback.
GEMINI_MODEL_ENV = os.environ.get("GEMINI_MODEL") or ""
GEMINI_MODEL_PREFERENCES = [
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
]
GEMINI_MIN_INTERVAL_S = 13  # Free tier: 5 req/min. 60 / 5 = 12s; add margin.

LOG_PATH = Path(__file__).parent / "jobs_log.csv"
LOG_FIELDS = [
    "timestamp",
    "job_id",
    "title",
    "company",
    "url",
    "score",
    "emailed",
    "reason",
]

RESEND_FROM = os.environ.get("RESEND_FROM") or "onboarding@resend.dev"
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL") or "yaminbinyoosuf@gmail.com"


COGEXT_API_KEY = os.environ.get("COGEXT_API_KEY") or ""
COGEXT_API_URL = "https://api.cogextai.com/api/v1"
# Fixed agent UUID for this job-agent process
COGEXT_AGENT_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"

PROFILE = """\
Name: Yamin Binyoosuf
Skills: AI voice agents (Sarvam AI), WhatsApp Business API (Meta Cloud API),
FastAPI/Python, React/TypeScript, PostgreSQL, Docker, AWS Lightsail,
Malayalam-language AI products.
Experience: Built and shipped a production clinic operating system solo —
Malayalam voice booking, WhatsApp automation, queue management — with real
users in Kerala, India.
Rate: $25-50/hr, open to project-based work.
Location: India, remote only.
Portfolio: thryvixai.com
Contact: yaminbinyoosuf@gmail.com / +91 8304881059
"""

EMAIL_TEMPLATE = """\
Hi [hiring manager/team],

I came across your posting for [role].

I've built production AI systems in India:
- Malayalam voice booking agent (Sarvam AI + FastAPI)
- WhatsApp Business API automation (Meta Cloud API)
- Full-stack clinic OS: React + FastAPI + PostgreSQL + AWS — solo,
  bootstrapped, live users

Available immediately for remote work.
Portfolio: thryvixai.com

Yamin
+91 8304881059
"""


# ---------------------------------------------------------------------------
# RemoteOK
# ---------------------------------------------------------------------------

USER_AGENT = "Mozilla/5.0 (compatible; job-agent/1.0)"


def fetch_remoteok_jobs() -> list[dict]:
    """Fetch the current RemoteOK listing feed. RemoteOK blocks requests
    without a browser-like User-Agent, and the first array element is a
    legal notice, not a job."""
    resp = requests.get(REMOTEOK_API_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return [
        {**job, "id": f"remoteok:{job['id']}", "source": "RemoteOK"}
        for job in data
        if isinstance(job, dict) and job.get("id")
    ]


def fetch_wwr_jobs() -> list[dict]:
    """Fetch WeWorkRemotely's programming feed and normalize to the same
    shape as RemoteOK: id, position, company, description, tags, url, epoch.
    RSS titles are 'Company Name: Role Title'."""
    resp = requests.get(WWR_RSS_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    jobs = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc_html = (item.findtext("description") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        if not guid:
            continue
        company, sep, position = title.partition(": ")
        if not sep:
            company, position = "", company
        epoch = None
        if pub:
            try:
                epoch = parsedate_to_datetime(pub).timestamp()
            except (TypeError, ValueError):
                pass
        jobs.append({
            "id": f"wwr:{guid}",
            "position": position.strip(),
            "company": company.strip(),
            "description": re.sub(r"<[^>]+>", " ", desc_html),
            "tags": [],
            "url": link,
            "epoch": epoch,
            "source": "WeWorkRemotely",
        })
    return jobs


def job_posted_at(job: dict) -> datetime | None:
    epoch = job.get("epoch")
    if epoch:
        try:
            return datetime.fromtimestamp(float(epoch), tz=timezone.utc)
        except (TypeError, ValueError):
            pass
    date_str = job.get("date")
    if date_str:
        try:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            pass
    return None


def matches_keywords(job: dict) -> bool:
    haystack = " ".join(
        str(job.get(field, "")) for field in ("position", "description", "tags")
    ).lower()
    return any(kw in haystack for kw in KEYWORDS)


def filter_recent_matching_jobs(jobs: list[dict]) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    filtered = []
    for job in jobs:
        posted_at = job_posted_at(job)
        if posted_at is None or posted_at < cutoff:
            continue
        if not matches_keywords(job):
            continue
        filtered.append(job)
    return filtered


# ---------------------------------------------------------------------------
# Gemini scoring + outreach drafting
# ---------------------------------------------------------------------------

def pick_gemini_model(client: genai.Client) -> str:
    """Pick a Gemini model this API key can actually call. Prefer the
    explicit GEMINI_MODEL env var, otherwise the first entry in
    GEMINI_MODEL_PREFERENCES that ListModels returns as available for
    generateContent. Falls back to the first preference and lets the
    request itself fail loudly."""
    available: set[str] = set()
    try:
        for m in client.models.list():
            name = (m.name or "").removeprefix("models/")
            methods = getattr(m, "supported_actions", None) or getattr(m, "supported_generation_methods", None) or []
            if "generateContent" in methods or not methods:
                available.add(name)
    except Exception as exc:  # noqa: BLE001
        print(f"  Model listing failed ({exc}); trying preferred model as-is.", file=sys.stderr)

    if GEMINI_MODEL_ENV:
        if not available or GEMINI_MODEL_ENV in available:
            return GEMINI_MODEL_ENV
        print(f"  GEMINI_MODEL='{GEMINI_MODEL_ENV}' not in ListModels; using it anyway.", file=sys.stderr)
        return GEMINI_MODEL_ENV

    for candidate in GEMINI_MODEL_PREFERENCES:
        if candidate in available:
            return candidate

    return GEMINI_MODEL_PREFERENCES[0]


def _build_score_prompt(job: dict) -> str:
    title = job.get("position", "Unknown role")
    company = job.get("company", "Unknown company")
    description = (job.get("description") or "")[:4000]
    tags = ", ".join(job.get("tags", []) or [])
    return f"""You are helping Yamin evaluate a remote job posting and draft outreach.

CANDIDATE PROFILE:
{PROFILE}

OUTREACH EMAIL TEMPLATE (match this tone and structure, adapt to the role):
{EMAIL_TEMPLATE}

JOB POSTING:
Title: {title}
Company: {company}
Tags: {tags}
Description: {description}

Score how well this candidate fits this job from 1-10, based on real skill
and experience overlap (not just keyword matches). Then draft a personalized
outreach email following the template's tone — replace [hiring manager/team]
and [role] appropriately, and tie 1-2 of Yamin's specific projects to what
this job actually needs. Keep the email under 150 words.

Respond with ONLY a JSON object, no other text, in exactly this shape:
{{"score": <integer 1-10>, "reason": "<one sentence>", "email_subject": "<subject line>", "email_body": "<email text>"}}"""


def _parse_json_response(raw: str) -> dict:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object in model output: {raw[:200]}")
    return json.loads(match.group(0))


def score_and_draft_deepseek(job: dict) -> dict:
    """Score a job and draft outreach using the DeepSeek API."""
    prompt = _build_score_prompt(job)
    resp = requests.post(
        DEEPSEEK_BASE_URL,
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": DEEPSEEK_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        },
        timeout=60,
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"].strip()
    return _parse_json_response(raw)


def score_and_draft(client, model: str, job: dict) -> dict:
    prompt = _build_score_prompt(job)
    response = client.models.generate_content(model=model, contents=prompt)
    raw = (response.text or "").strip()
    return _parse_json_response(raw)


# ---------------------------------------------------------------------------
# Resend
# ---------------------------------------------------------------------------

def send_email_via_resend(api_key: str, subject: str, body: str) -> bool:
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": RESEND_FROM,
            "to": [NOTIFY_EMAIL],
            "subject": subject,
            "text": body,
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        print(f"  Resend error {resp.status_code}: {resp.text}", file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def load_seen_job_ids() -> set[str]:
    """Return job_ids we've already finished with. A row is 'done' only if
    we made a final decision on it — a scoring error or a strong match whose
    email failed to send should be retried next run, not silently skipped."""
    if not LOG_PATH.exists():
        return set()
    seen: set[str] = set()
    with LOG_PATH.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("reason", "").startswith("error:"):
                continue
            try:
                score = int(row.get("score") or 0)
            except ValueError:
                score = 0
            emailed = row.get("emailed", "").lower() == "true"
            if score >= SCORE_THRESHOLD and not emailed:
                continue
            seen.add(row["job_id"])
    return seen


def append_log(rows: list[dict]) -> None:
    is_new = not LOG_PATH.exists()
    with LOG_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerows(rows)



# ---------------------------------------------------------------------------
# COGEXT commitment tracking
# ---------------------------------------------------------------------------

def track_commitment(job_id: str, title: str, company: str, url: str, score: int) -> None:
    """Report a job application commitment to COGEXT."""
    if not COGEXT_API_KEY:
        return
    session_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, job_id))
    message = (
        f"I will apply to the {title} role at {company} (score {score}/10). "
        f"I will send the outreach email and follow up within 48 hours. Apply URL: {url}"
    )
    try:
        resp = requests.post(
            f"{COGEXT_API_URL}/ingest",
            headers={"Authorization": f"Bearer {COGEXT_API_KEY}", "Content-Type": "application/json"},
            json={"source_agent_id": COGEXT_AGENT_ID, "session_id": session_id, "message": message},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            count = len(data) if isinstance(data, list) else data.get("count", "?")
            print(f"    COGEXT: {count} commitment(s) tracked")
        else:
            print(f"    COGEXT: ingest returned {resp.status_code}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"    COGEXT: tracking failed ({exc})", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    resend_key = os.environ.get("RESEND_API_KEY")
    if not resend_key:
        sys.exit("RESEND_API_KEY is not set")

    # Pick AI backend: DeepSeek (preferred) or Gemini fallback.
    use_deepseek = bool(DEEPSEEK_API_KEY)
    client = None
    gemini_model = None
    if use_deepseek:
        print(f"Using DeepSeek model: {DEEPSEEK_MODEL}")
    else:
        gemini_key = os.environ.get("GEMINI_API_KEY")
        if not gemini_key:
            sys.exit("Set DEEPSEEK_API_KEY or GEMINI_API_KEY")
        if _genai is None:
            sys.exit("google-genai package not installed; run: pip install google-genai")
        client = _genai.Client(api_key=gemini_key)
        gemini_model = pick_gemini_model(client)
        print(f"Using Gemini model: {gemini_model}")

    print("Fetching listings...")
    all_jobs: list[dict] = []
    for name, fetcher in (("RemoteOK", fetch_remoteok_jobs), ("WeWorkRemotely", fetch_wwr_jobs)):
        try:
            jobs = fetcher()
            print(f"  {name}: {len(jobs)} listings")
            all_jobs.extend(jobs)
        except Exception as exc:  # noqa: BLE001 - one source down shouldn't kill the run
            print(f"  {name}: fetch failed: {exc}", file=sys.stderr)
    print(f"  {len(all_jobs)} total listings")

    candidates = filter_recent_matching_jobs(all_jobs)
    print(f"  {len(candidates)} match keywords and posted in last {LOOKBACK_HOURS}h")

    seen_ids = load_seen_job_ids()
    new_candidates = [j for j in candidates if str(j["id"]) not in seen_ids]
    print(f"  {len(new_candidates)} not yet processed")

    log_rows = []
    emailed_count = 0
    last_gemini_call = 0.0

    for job in new_candidates:
        job_id = str(job["id"])
        title = job.get("position", "Unknown role")
        company = job.get("company", "Unknown company")
        url = job.get("url") or ""

        if not use_deepseek:
            # Pace Gemini requests — free tier: 5 req/min.
            wait = GEMINI_MIN_INTERVAL_S - (time.monotonic() - last_gemini_call)
            if wait > 0:
                time.sleep(wait)
            last_gemini_call = time.monotonic()

        try:
            result = (
                score_and_draft_deepseek(job)
                if use_deepseek
                else score_and_draft(client, gemini_model, job)
            )
        except Exception as exc:  # noqa: BLE001 - log and continue on any API hiccup
            print(f"  [{title} @ {company}] scoring failed: {exc}", file=sys.stderr)
            log_rows.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "job_id": job_id,
                "title": title,
                "company": company,
                "url": url,
                "score": "",
                "emailed": False,
                "reason": f"error: {exc}",
            })
            continue

        score = result.get("score", 0)
        reason = result.get("reason", "")
        print(f"  [{score}/10] {title} @ {company} — {reason}")

        emailed = False
        if score >= SCORE_THRESHOLD:
            track_commitment(job_id, title, company, url, score)
            subject = result.get("email_subject") or f"AI Agent + FastAPI Developer — Available for {title}"
            body = f"{result.get('email_body', '')}\n\n---\nJob: {title} @ {company}\nApply: {url}\nMatch score: {score}/10\n"
            emailed = send_email_via_resend(resend_key, subject, body)
            if emailed:
                emailed_count += 1

        log_rows.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "job_id": job_id,
            "title": title,
            "company": company,
            "url": url,
            "score": score,
            "emailed": emailed,
            "reason": reason,
        })

    append_log(log_rows)
    print(f"Done. {emailed_count} outreach draft(s) emailed to {NOTIFY_EMAIL}.")
    print(f"Full log: {LOG_PATH}")


if __name__ == "__main__":
    main()
