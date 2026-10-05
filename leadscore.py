"""Opportunity scoring for paid, short, task-based engineering work.

This module is deliberately pure: no network, no disk, no LLM calls. Everything
here is a function of the normalised job dict produced by ``job_agent._job``
plus, optionally, the structured result of the LLM screening pass. That keeps
the filtering and ranking logic fully unit-testable.

The score answers one question:

    "How likely is this to become a realistic paid task that THIS engineer
     can win and complete quickly?"

It deliberately does not answer "how interesting is this job?".

Weights sum to 100:

    skill match            25
    task clarity           15
    payment/budget quality 15
    short duration         10
    fixed-price/one-time   10
    freshness              10
    low competition         5
    client quality          5
    low interview risk      5
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Weights
# ---------------------------------------------------------------------------

WEIGHTS: dict[str, int] = {
    "skill_match": 25,
    "task_clarity": 15,
    "payment_quality": 15,
    "short_duration": 10,
    "fixed_price": 10,
    "freshness": 10,
    "low_competition": 5,
    "client_quality": 5,
    "low_interview": 5,
}

# ---------------------------------------------------------------------------
# Candidate skill model, derived from the resume
# ---------------------------------------------------------------------------

# Higher weight = more central to the candidate's proven, shipped stack.
SKILL_WEIGHTS: dict[str, int] = {
    # Core shipped stack
    "fastapi": 10,
    "python": 8,
    "postgresql": 8,
    "sqlalchemy": 7,
    "rest api": 7,
    "api integration": 7,
    "llm": 7,
    "openai": 7,
    # Strong adjacent
    "ai agent": 6,
    "webhook": 6,
    "playwright": 6,
    "scraping": 6,
    "scraper": 6,
    "bug fix": 6,
    "api debugging": 6,
    "flask": 6,
    "docker": 5,
    "automation": 5,
    "react": 5,
    "typescript": 5,
    "prompt engineering": 5,
    "rag": 5,
    "deepseek": 5,
    "data processing": 5,
    "debugging": 5,
    "aws": 5,
    "whatsapp": 5,
    # Useful
    "javascript": 4,
    "groq": 4,
    "llama": 4,
    "langchain": 4,
    "embeddings": 4,
    "chatbot": 4,
    "n8n": 4,
    "workflow automation": 4,
    "telegram": 4,
    "deployment": 4,
    "sql": 4,
    "selenium": 4,
    "vite": 3,
    "nginx": 3,
    "cloudflare": 3,
    "vercel": 3,
    "linux": 3,
    "github actions": 3,
    "ci/cd": 3,
    "razorpay": 3,
    "gmail": 3,
    "google places": 3,
    "authentication": 3,
    "state machine": 3,
    "testing": 3,
    "security": 3,
}

# A job hitting this much weighted skill is treated as a full match.
SKILL_REFERENCE = 28

# ---------------------------------------------------------------------------
# Text signals
# ---------------------------------------------------------------------------

# Explicit short-task language — the strongest indicator of a winnable task.
TASK_SIGNALS = (
    "fixed price",
    "fixed-price",
    "one-time",
    "one time",
    "small task",
    "quick task",
    "bug fix",
    "bugfix",
    "fix the",
    "fix a bug",
    "debug",
    "integrate",
    "integration",
    "api endpoint",
    "endpoint",
    "script",
    "small script",
    "automate",
    "automation",
    "scrape",
    "scraper",
    "scraping",
    "migrate",
    "convert",
    "add a feature",
    "implement",
    "set up",
    "setup",
    "deploy",
    "deployment",
    "refactor",
    "webhook",
    "acceptance criteria",
    "deliverable",
    "deliverables",
    "requirements are",
    "must be able to",
    "as soon as possible",
    "start immediately",
    "immediate start",
    "milestone",
    "proof of concept",
    "poc",
    "mvp",
)

# Signals that the work is open-ended, long, or not a discrete deliverable.
VAGUE_SIGNALS = (
    "ongoing",
    "long term",
    "long-term",
    "longterm",
    "as needed",
    "on demand",
    "various tasks",
    "multiple projects",
    "rockstar",
    "ninja",
    "guru",
    "wear many hats",
    "full time",
    "full-time",
    "permanent",
    "contract to hire",
    "contract-to-hire",
    "temp to perm",
    "internship",
    "intern ",
    "unpaid",
    "volunteer",
    "equity only",
    "equity-only",
    "revenue share",
    "revenue-share",
    "commission only",
    "commission-only",
    "months",
    "12+ months",
    "6+ months",
    "year-long",
    "year long",
)

# Roles that are not hands-on engineering deliverables.
NON_ENGINEERING_SIGNALS = (
    "sales",
    "account executive",
    "marketing",
    "recruiter",
    "talent acquisition",
    "customer support",
    "customer success",
    "virtual assistant",
    "data entry",
    "copywriter",
    "content writer",
    "transcri",
    "bookkeep",
    "payroll",
    "cold call",
    "lead generation",
    "business development",
)

# Qualifications the candidate demonstrably does not hold, or clearances.
DISQUALIFYING_CREDENTIALS = (
    "phd required",
    "ph.d. required",
    "doctorate required",
    "md required",
    "board certified",
    "security clearance",
    "top secret",
    "ts/sci",
    "active clearance",
    "licensed attorney",
    "cpa required",
    "registered nurse",
    "10+ years",
    "12+ years",
    "15+ years",
    "8+ years",
)

# Low-effort scam / exploit patterns.
SCAM_SIGNALS = (
    "pay a fee",
    "registration fee",
    "processing fee",
    "upfront payment",
    "send money",
    "wire transfer",
    "gift card",
    "crypto payment only",
    "no experience needed earn",
    "earn $",
    "make money fast",
    "pyramid",
    "mlm",
    "western union",
    "paypal friends and family",
    "test task unpaid",
    "unpaid trial",
    "unpaid test",
)

# Explicit no-interview language. Presence strongly supports the candidate's
# hard requirement; absence is NOT a rejection reason on its own.
NO_INTERVIEW_SIGNALS = (
    "no interview",
    "without interview",
    "no interviews",
    "no whiteboard",
    "no coding interview",
    "no technical interview",
    "take-home only",
    "take home only",
    "paid trial task",
    "paid test task",
    "start immediately",
    "immediate start",
    "no calls required",
    "chat only",
)

INTERVIEW_HEAVY_SIGNALS = (
    "interview process",
    "multiple rounds",
    "several rounds",
    "technical interview",
    "coding interview",
    "whiteboard",
    "panel interview",
    "onsite interview",
    "phone screen",
    "video interview",
    "assessment centre",
    "assessment center",
)

# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------

# Approximate static rates, used only for rough comparison. Not accounting.
CURRENCY_TO_USD: dict[str, float] = {
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
    "AED": 0.27,
    "SAR": 0.27,
    "TRY": 0.029,
    "MYR": 0.22,
    "IDR": 0.000062,
    "VND": 0.000040,
    "THB": 0.028,
    "JPY": 0.0064,
    "CNY": 0.14,
    "HKD": 0.128,
    "KRW": 0.00072,
    "ILS": 0.27,
    "RON": 0.22,
    "HUF": 0.0027,
    "CZK": 0.043,
    "UAH": 0.024,
    "RUB": 0.011,
}

_CURRENCY_SYMBOLS = {
    "$": "USD",
    "₹": "INR",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
}

_MONEY_RE = re.compile(
    r"(?P<sym>[$₹€£¥])\s?(?P<lo>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s?(?:-|–|to)\s?(?P<sym2>[$₹€£¥])?\s?(?P<hi>\d[\d,]*(?:\.\d+)?))?"
)
_CODE_RE = re.compile(
    r"\b(?P<code>USD|EUR|GBP|INR|AUD|CAD|SGD|NZD|PLN|CHF|SEK|ZAR|BRL|MXN|PHP|PKR|BDT|NGN|"
    r"AED|SAR|TRY|MYR|IDR|VND|THB|JPY|CNY|HKD|KRW|ILS|RON|HUF|CZK|UAH|RUB)\b"
    r"\s?(?P<lo>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s?(?:-|–|to)\s?(?P<hi>\d[\d,]*(?:\.\d+)?))?",
    re.I,
)

_HOURLY_RE = re.compile(r"(?:/|per\s)\s*(?:hour|hr)\b|hourly|/\s?hr\b", re.I)
_MONTHLY_RE = re.compile(r"(?:/|per\s)\s*(?:month|mo)\b|monthly", re.I)
_FIXED_RE = re.compile(r"fixed[\s-]?price|fixed[\s-]?budget|\bfixed\b", re.I)

_HOURS_RE = re.compile(
    r"(?P<lo>\d+(?:\.\d+)?)\s?(?:-|–|to)\s?(?P<hi>\d+(?:\.\d+)?)\s?(?P<unit>hour|hr|day|week)",
    re.I,
)
_HOURS_SINGLE_RE = re.compile(
    r"(?:within|about|approx(?:imately)?|around|under|less than)?\s?"
    r"(?P<n>\d+(?:\.\d+)?)\s?(?P<unit>hour|hr|day|week)s?\b",
    re.I,
)
_DURATION_WORDS = {
    "a few hours": 3.0,
    "few hours": 3.0,
    "couple of hours": 2.0,
    "an hour": 1.0,
    "half a day": 4.0,
    "a day": 8.0,
    "one day": 8.0,
    "few days": 16.0,
    "a few days": 16.0,
    "a week": 30.0,
    "one week": 30.0,
}


def _to_float(text: str) -> float:
    """Parse a money figure. Defensive: a stray separator must never crash a run."""
    cleaned = (text or "").replace(",", "").strip()
    if not cleaned:
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _usd(amount: float, currency: str) -> float:
    return amount * CURRENCY_TO_USD.get(currency.upper(), 1.0)


# ---------------------------------------------------------------------------
# Extracted facts
# ---------------------------------------------------------------------------


@dataclass
class Budget:
    """Best-effort budget reading from a listing."""

    low: float = 0.0
    high: float = 0.0
    currency: str = "USD"
    hourly: bool = False
    monthly: bool = False
    fixed: bool = False
    source: str = "unknown"

    @property
    def usd_low(self) -> float:
        return _usd(self.low, self.currency)

    @property
    def usd_high(self) -> float:
        return _usd(self.high, self.currency)

    @property
    def usd_estimate(self) -> float:
        """A single representative USD figure (midpoint, or the only value)."""
        if self.low and self.high:
            return (self.usd_low + self.usd_high) / 2
        return self.usd_high or self.usd_low

    @property
    def is_known(self) -> bool:
        return bool(self.low or self.high)

    def describe(self) -> str:
        if not self.is_known:
            return "not stated"
        if self.low and self.high and self.low != self.high:
            base = f"{self.currency} {self.low:,.0f}-{self.high:,.0f}"
        else:
            base = f"{self.currency} {self.high or self.low:,.0f}"
        if self.currency != "USD":
            base += f" (~${self.usd_estimate:,.0f})"
        if self.hourly:
            base += "/hr"
        elif self.monthly:
            base += "/month"
        elif self.fixed:
            base += " fixed"
        return base


def parse_budget(text: str) -> Budget | None:
    """Read a budget out of free text. Returns None when nothing is found."""
    if not text:
        return None
    best: Budget | None = None

    match = _MONEY_RE.search(text)
    if match:
        currency = _CURRENCY_SYMBOLS.get(match.group("sym"), "USD")
        low = _to_float(match.group("lo"))
        high = _to_float(match.group("hi")) if match.group("hi") else low
        best = Budget(low=min(low, high), high=max(low, high), currency=currency,
                      source="symbol")

    code_match = _CODE_RE.search(text)
    if code_match:
        currency = code_match.group("code").upper()
        low = _to_float(code_match.group("lo"))
        high = _to_float(code_match.group("hi")) if code_match.group("hi") else low
        candidate = Budget(low=min(low, high), high=max(low, high), currency=currency,
                           source="code")
        # Prefer whichever reading implies the larger real value, since a
        # symbol match can pick up a stray "$" elsewhere in the text.
        if best is None or candidate.usd_estimate > best.usd_estimate:
            best = candidate

    if best is None:
        return None

    if _HOURLY_RE.search(text):
        best.hourly = True
    elif _MONTHLY_RE.search(text):
        best.monthly = True
    elif _FIXED_RE.search(text):
        best.fixed = True
    return best


def estimate_hours(job: dict, budget: Budget | None = None) -> float:
    """Estimate effort in hours.

    Prefers an explicit statement in the listing, then falls back to a
    budget-and-scope heuristic. Deliberately conservative: this drives both the
    duration score and the implied hourly rate."""
    text = f"{job.get('title', '')} {job.get('description', '')}".lower()

    match = _HOURS_RE.search(text)
    if match:
        lo = float(match.group("lo"))
        hi = float(match.group("hi"))
        unit = match.group("unit").lower()
        factor = 1.0 if unit in ("hour", "hr") else (8.0 if unit == "day" else 30.0)
        return max(0.5, ((lo + hi) / 2) * factor)

    for phrase, hours in _DURATION_WORDS.items():
        if phrase in text:
            return hours

    single = _HOURS_SINGLE_RE.search(text)
    if single:
        n = float(single.group("n"))
        unit = single.group("unit").lower()
        factor = 1.0 if unit in ("hour", "hr") else (8.0 if unit == "day" else 30.0)
        return max(0.5, n * factor)

    # Heuristic fallback from budget size and scope language.
    if budget and budget.is_known:
        usd = budget.usd_estimate
        if budget.hourly:
            return 8.0
        if usd <= 60:
            return 3.0
        if usd <= 150:
            return 6.0
        if usd <= 350:
            return 12.0
        if usd <= 800:
            return 24.0
        if usd <= 2000:
            return 40.0
        return 60.0

    # No budget and no duration: judge purely on scope words.
    if any(word in text for word in ("quick", "small", "minor", "simple", "one bug")):
        return 4.0
    if any(word in text for word in ("build", "develop", "platform", "system", "full app")):
        return 40.0
    return 16.0


def classify_interview(job: dict, llm_result: dict | None = None) -> str:
    """NONE / LOW / MEDIUM / HIGH.

    Source metadata wins where it exists (Mercor publishes it). Otherwise the
    listing text decides, with the LLM's own read as a tiebreaker."""
    if job.get("no_interview") is True and job.get("work_type") == "task":
        return "NONE"

    text = f"{job.get('title', '')} {job.get('description', '')}".lower()
    explicit_none = any(sig in text for sig in NO_INTERVIEW_SIGNALS)
    heavy = any(sig in text for sig in INTERVIEW_HEAVY_SIGNALS)

    if explicit_none and not heavy:
        return "NONE"

    model = str((llm_result or {}).get("interview_process") or "").strip().lower()
    if model in ("none",):
        return "NONE"
    if model in ("light",):
        return "LOW"

    work_type = job.get("work_type") or "full_time"
    if work_type == "task":
        # Bid-and-start marketplaces have no interview loop by construction.
        return "LOW" if job.get("source") in ("Freelancer.com", "HN Freelancer") else "NONE"
    if job.get("no_interview") is True:
        return "LOW"

    if heavy:
        return "HIGH"
    if model in ("standard",):
        return "HIGH" if work_type == "full_time" else "MEDIUM"
    if work_type == "contract":
        return "MEDIUM"
    return "MEDIUM" if work_type == "full_time" else "LOW"


# ---------------------------------------------------------------------------
# Component scores (each 0-100)
# ---------------------------------------------------------------------------


def skill_match(job: dict) -> tuple[int, list[str]]:
    """Weighted overlap with the candidate's proven stack."""
    haystack = " ".join(
        [
            job.get("title", ""),
            job.get("description", ""),
            " ".join(job.get("tags", []) or []),
        ]
    ).lower()
    matched = [skill for skill in SKILL_WEIGHTS if skill in haystack]
    total = sum(SKILL_WEIGHTS[skill] for skill in matched)
    score = min(100, round(100 * total / SKILL_REFERENCE))
    matched.sort(key=lambda s: -SKILL_WEIGHTS[s])
    return score, matched


def task_clarity(job: dict) -> tuple[int, list[str]]:
    """Is there a concrete, bounded deliverable?"""
    text = f"{job.get('title', '')} {job.get('description', '')}".lower()
    hits = [sig for sig in TASK_SIGNALS if sig in text]
    vague = [sig for sig in VAGUE_SIGNALS if sig in text]

    score = min(70, len(hits) * 12)
    if job.get("work_type") == "task":
        score += 15
    if job.get("salary"):
        score += 10  # a stated budget usually means a scoped brief
    if len(job.get("description", "")) > 400:
        score += 5
    score -= len(vague) * 18
    return max(0, min(100, score)), hits


def payment_quality(budget: Budget | None, hours: float) -> int:
    """Absolute budget, and value per hour once effort is estimated."""
    if budget is None or not budget.is_known:
        return 20  # unknown is weak, not fatal

    usd = budget.usd_estimate
    if budget.hourly:
        per_hour = usd
    elif budget.monthly:
        # A monthly retainer is long-term work; treat the implied hourly as low.
        per_hour = usd / max(hours, 40.0)
    else:
        per_hour = usd / max(hours, 0.5)

    if per_hour >= 60:
        rate_score = 100
    elif per_hour >= 40:
        rate_score = 90
    elif per_hour >= 25:
        rate_score = 75
    elif per_hour >= 15:
        rate_score = 55
    elif per_hour >= 8:
        rate_score = 35
    else:
        rate_score = 10

    # Very small absolute budgets are rarely worth the transaction cost, even
    # when the implied hourly rate looks fine on a tiny task.
    if budget.monthly:
        return min(rate_score, 45)
    if not budget.hourly and usd < 50:
        return min(rate_score, 40)
    return rate_score


def short_duration(hours: float) -> int:
    if hours <= 4:
        return 100
    if hours <= 8:
        return 90
    if hours <= 16:
        return 75
    if hours <= 24:
        return 60
    if hours <= 40:
        return 40
    if hours <= 80:
        return 20
    return 0


def fixed_price_score(job: dict, budget: Budget | None) -> int:
    """Fixed-price one-time work is the target; long retainers are not."""
    work_type = job.get("work_type") or "full_time"
    if work_type == "full_time":
        return 0
    if budget is not None and budget.monthly:
        return 20
    if budget is not None and budget.hourly:
        return 55
    if (budget is not None and budget.fixed) or work_type == "task":
        return 100
    if work_type == "contract":
        return 45
    return 25


def freshness(job: dict, now: datetime | None = None) -> int:
    posted = job.get("posted_at")
    if not posted:
        return 45  # undated: neutral, do not punish
    now = now or datetime.now(timezone.utc)
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (now - posted).total_seconds() / 3600)
    if age_hours <= 6:
        return 100
    if age_hours <= 24:
        return 90
    if age_hours <= 72:
        return 70
    if age_hours <= 168:
        return 45
    if age_hours <= 720:
        return 20
    return 5


def low_competition(job: dict) -> int:
    bids = job.get("competition")
    if bids is None:
        return 50  # unknown
    if bids <= 2:
        return 100
    if bids <= 5:
        return 85
    if bids <= 10:
        return 65
    if bids <= 20:
        return 45
    if bids <= 40:
        return 25
    if bids <= 80:
        return 10
    return 0


def client_quality(job: dict) -> int:
    """Only uses signals actually present on the listing."""
    score = 50
    if job.get("payment_verified"):
        score += 25
    if job.get("client_hire_rate") is not None:
        try:
            score += int(round(float(job["client_hire_rate"]) * 25))
        except (TypeError, ValueError):
            pass
    if job.get("competition") is not None:
        score += 5  # at least the platform shows us the field
    return max(0, min(100, score))


_INTERVIEW_SCORES = {"NONE": 100, "LOW": 80, "MEDIUM": 40, "HIGH": 0}


def low_interview(interview: str) -> int:
    return _INTERVIEW_SCORES.get(interview, 50)


# ---------------------------------------------------------------------------
# Hard rejection
# ---------------------------------------------------------------------------

REJECTION_RULES: tuple[str, ...] = (
    "full_time",
    "internship",
    "unpaid",
    "volunteer",
    "contract_to_hire",
    "long_term",
    "too_many_hours",
    "no_deliverable",
    "no_budget",
    "location_excluded",
    "unqualified",
    "recruitment_process",
    "commission_only",
    "scam",
    "budget_too_low",
    "not_engineering",
    "blocked_title",
    "no_keyword_match",
    "expired",
)


def rejection_reasons(
    job: dict,
    *,
    now: datetime | None = None,
    min_hourly_value: float = 8.0,
    require_budget: bool = False,
    expiry_days: int = 120,
) -> list[str]:
    """Apply the hard rejection rules. Any reason returned means: drop it.

    Deliberately does NOT reject simply because a listing fails to promise
    "no interview" — interview risk is scored, not used as a gate."""
    reasons: list[str] = []
    text = f"{job.get('title', '')} {job.get('description', '')}".lower()
    work_type = job.get("work_type") or "full_time"

    if work_type == "full_time":
        reasons.append("full_time")

    for phrase in ("internship", "intern ", "trainee program"):
        if phrase in text:
            reasons.append("internship")
            break
    if re.search(r"\bunpaid\b|\bno pay\b|\bwithout pay\b|\bpro bono\b", text):
        reasons.append("unpaid")
    if re.search(r"\bvolunteer\b|\bvoluntary\b", text):
        reasons.append("volunteer")
    if re.search(r"contract[\s-]to[\s-]hire|temp[\s-]to[\s-]perm|c2h\b", text):
        reasons.append("contract_to_hire")
    if re.search(r"\blong[\s-]?term\b|\bongoing\b|\bmonths? of work\b|\b\d{1,2}\+? months\b", text):
        reasons.append("long_term")
    if re.search(r"\bcommission[\s-]only\b|\brevenue[\s-]?share[\s-]?only\b|\bequity[\s-]?only\b", text):
        reasons.append("commission_only")
    if re.search(r"\binternship\b|\bpermanent\b|\bfull[\s-]?time\b", text) and work_type == "full_time":
        reasons.append("full_time")

    hours = estimate_hours(job)
    if hours > 160:
        reasons.append("long_term")

    # 20+ hours/week is only acceptable when explicitly task-based and well paid.
    per_week = job.get("hours_per_week")
    if per_week:
        try:
            if float(per_week) >= 20 and not (
                work_type == "task" and (job.get("salary") or "")
            ):
                reasons.append("too_many_hours")
        except (TypeError, ValueError):
            pass

    budget = parse_budget(job.get("salary") or "") or parse_budget(job.get("description") or "")
    clarity, _ = task_clarity(job)

    if budget is None or not budget.is_known:
        if require_budget:
            reasons.append("no_budget")
    else:
        per_hour = (
            budget.usd_estimate
            if budget.hourly
            else budget.usd_estimate / max(hours, 0.5)
        )
        if not budget.monthly and per_hour < min_hourly_value:
            reasons.append("budget_too_low")

    if clarity < 20:
        reasons.append("no_deliverable")

    if job.get("location_excluded"):
        reasons.append("location_excluded")

    if any(sig in text for sig in DISQUALIFYING_CREDENTIALS):
        reasons.append("unqualified")
    if any(sig in text for sig in SCAM_SIGNALS):
        reasons.append("scam")
    if any(sig in text for sig in NON_ENGINEERING_SIGNALS) and clarity < 50:
        reasons.append("not_engineering")

    posted = job.get("posted_at")
    if posted:
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        age_days = ((now or datetime.now(timezone.utc)) - posted).days
        if age_days > expiry_days:
            reasons.append("expired")

    if job.get("expired") is True:
        reasons.append("expired")

    # De-duplicate while preserving rule order.
    seen: set[str] = set()
    ordered: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            ordered.append(reason)
    return ordered


# ---------------------------------------------------------------------------
# Composite score
# ---------------------------------------------------------------------------


@dataclass
class Evaluation:
    """The full judgement on one listing."""

    opportunity_score: int = 0
    components: dict[str, int] = field(default_factory=dict)
    matched_skills: list[str] = field(default_factory=list)
    clarity_hits: list[str] = field(default_factory=list)
    budget: Budget | None = None
    estimated_hours: float = 0.0
    implied_hourly_usd: float = 0.0
    skill_match_pct: int = 0
    interview: str = "MEDIUM"
    rejections: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    positive_signals: list[str] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return bool(self.rejections)

    @property
    def priority(self) -> str:
        if self.opportunity_score >= 85:
            return "HIGH PRIORITY"
        if self.opportunity_score >= 70:
            return "HIGH"
        if self.opportunity_score >= 55:
            return "MEDIUM-HIGH"
        if self.opportunity_score >= 40:
            return "MEDIUM"
        return "LOW"

    @property
    def expected_value_usd(self) -> float:
        return self.budget.usd_estimate if self.budget and self.budget.is_known else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity_score": self.opportunity_score,
            "priority": self.priority,
            "components": dict(self.components),
            "matched_skills": list(self.matched_skills),
            "skill_match_pct": self.skill_match_pct,
            "interview": self.interview,
            "estimated_hours": round(self.estimated_hours, 1),
            "implied_hourly_usd": round(self.implied_hourly_usd, 2),
            "budget": self.budget.describe() if self.budget else "not stated",
            "budget_usd": round(self.expected_value_usd, 2),
            "rejections": list(self.rejections),
            "red_flags": list(self.red_flags),
            "positive_signals": list(self.positive_signals),
        }


def evaluate(
    job: dict,
    llm_result: dict | None = None,
    *,
    now: datetime | None = None,
    min_hourly_value: float = 8.0,
    require_budget: bool = False,
) -> Evaluation:
    """Produce the full evaluation for one listing."""
    budget = parse_budget(job.get("salary") or "") or parse_budget(
        job.get("description") or ""
    )
    hours = estimate_hours(job, budget)
    skills_score, matched = skill_match(job)
    clarity, clarity_hits = task_clarity(job)
    interview = classify_interview(job, llm_result)

    components = {
        "skill_match": skills_score,
        "task_clarity": clarity,
        "payment_quality": payment_quality(budget, hours),
        "short_duration": short_duration(hours),
        "fixed_price": fixed_price_score(job, budget),
        "freshness": freshness(job, now),
        "low_competition": low_competition(job),
        "client_quality": client_quality(job),
        "low_interview": low_interview(interview),
    }
    total = sum(WEIGHTS[key] * value / 100 for key, value in components.items())

    # The LLM screen can veto or endorse, but only within a bounded band: the
    # deterministic signals stay in charge of the ranking.
    model_score = None
    if llm_result:
        try:
            model_score = int(llm_result.get("score"))
        except (TypeError, ValueError):
            model_score = None
    if model_score is not None:
        # model score is 1-10 -> map to a -8..+8 adjustment
        total += (model_score - 6) * 8 / 5

    if budget and budget.is_known:
        hourly = (
            budget.usd_estimate
            if budget.hourly
            else budget.usd_estimate / max(hours, 0.5)
        )
    else:
        hourly = 0.0

    red_flags: list[str] = []
    positives: list[str] = []
    if interview in ("HIGH", "MEDIUM"):
        red_flags.append(f"{interview.lower()} interview likelihood")
    if budget and budget.is_known and budget.usd_estimate < 100 and not budget.hourly:
        red_flags.append("small absolute budget")
    if job.get("competition") and job["competition"] > 40:
        red_flags.append(f"{job['competition']} competing proposals")
    if not (budget and budget.is_known):
        red_flags.append("no stated budget")

    if budget and budget.hourly:
        positives.append("hourly rate stated")
    if budget and (budget.fixed or job.get("work_type") == "task"):
        positives.append("fixed-price / one-time task")
    if hours <= 16:
        positives.append(f"short scope (~{hours:g}h)")
    if matched:
        positives.append("matches: " + ", ".join(matched[:5]))
    if job.get("competition") is not None and job["competition"] <= 5:
        positives.append(f"only {job['competition']} competing proposals")
    if interview in ("NONE", "LOW"):
        positives.append(f"{interview.lower()} interview likelihood")

    return Evaluation(
        opportunity_score=max(0, min(100, round(total))),
        components=components,
        matched_skills=matched,
        clarity_hits=clarity_hits,
        budget=budget,
        estimated_hours=hours,
        implied_hourly_usd=hourly,
        skill_match_pct=skills_score,
        interview=interview,
        rejections=rejection_reasons(
            job,
            now=now,
            min_hourly_value=min_hourly_value,
            require_budget=require_budget,
        ),
        red_flags=red_flags,
        positive_signals=positives,
    )
