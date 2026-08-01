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
"""

import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import google.generativeai as genai

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REMOTEOK_API_URL = "https://remoteok.com/api"
KEYWORDS = ["ai agent", "fastapi", "whatsapp", "voice agent", "llm", "python"]
SCORE_THRESHOLD = 7
LOOKBACK_HOURS = 24
GEMINI_MODEL = "gemini-1.5-flash"

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

def fetch_remoteok_jobs() -> list[dict]:
    """Fetch the current RemoteOK listing feed. RemoteOK blocks requests
    without a browser-like User-Agent, and the first array element is a
    legal notice, not a job."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; job-agent/1.0)"}
    resp = requests.get(REMOTEOK_API_URL, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return [job for job in data if isinstance(job, dict) and job.get("id")]


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

def score_and_draft(model: genai.GenerativeModel, job: dict) -> dict:
    title = job.get("position", "Unknown role")
    company = job.get("company", "Unknown company")
    description = (job.get("description") or "")[:4000]
    tags = ", ".join(job.get("tags", []) or [])

    prompt = f"""You are helping Yamin evaluate a remote job posting and draft outreach.

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

    response = model.generate_content(prompt)
    raw = response.text.strip()
    # Model sometimes wraps JSON in a ```json fence despite instructions — strip it.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)


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
    if not LOG_PATH.exists():
        return set()
    with LOG_PATH.open(newline="", encoding="utf-8") as f:
        return {row["job_id"] for row in csv.DictReader(f)}


def append_log(rows: list[dict]) -> None:
    is_new = not LOG_PATH.exists()
    with LOG_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    gemini_key = os.environ.get("GEMINI_API_KEY")
    resend_key = os.environ.get("RESEND_API_KEY")
    if not gemini_key:
        sys.exit("GEMINI_API_KEY is not set")
    if not resend_key:
        sys.exit("RESEND_API_KEY is not set")

    genai.configure(api_key=gemini_key)
    model = genai.GenerativeModel(GEMINI_MODEL)

    print("Fetching RemoteOK listings...")
    all_jobs = fetch_remoteok_jobs()
    print(f"  {len(all_jobs)} total listings")

    candidates = filter_recent_matching_jobs(all_jobs)
    print(f"  {len(candidates)} match keywords and posted in last {LOOKBACK_HOURS}h")

    seen_ids = load_seen_job_ids()
    new_candidates = [j for j in candidates if str(j["id"]) not in seen_ids]
    print(f"  {len(new_candidates)} not yet processed")

    log_rows = []
    emailed_count = 0

    for job in new_candidates:
        job_id = str(job["id"])
        title = job.get("position", "Unknown role")
        company = job.get("company", "Unknown company")
        url = job.get("url") or f"https://remoteok.com/remote-jobs/{job_id}"

        try:
            result = score_and_draft(model, job)
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
            subject = result.get("email_subject") or f"AI Agent + FastAPI Developer — Available for {title}"
            body = f"{result.get('email_body', '')}\n\n---\nJob: {title} @ {company}\nApply: {url}\nMatch score: {score}/10\n"
            emailed = send_email_via_resend(resend_key, subject, body)
            if emailed:
                emailed_count += 1
            time.sleep(1)  # be polite to the Resend API

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

        time.sleep(1)  # be polite to the Gemini API

    append_log(log_rows)
    print(f"Done. {emailed_count} outreach draft(s) emailed to {NOTIFY_EMAIL}.")
    print(f"Full log: {LOG_PATH}")


if __name__ == "__main__":
    main()
