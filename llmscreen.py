"""The DeepSeek final-judgment layer.

DeepSeek is *not* the scraper, the filter, the database or the scoring engine.
It is a bounded, cached, strictly-schema'd judgment layer that runs only on the
small set of candidates the deterministic funnel has already shortlisted.

Everything here is pure — prompt construction, strict validation and the
bounded score adjustment — so it can be tested without any network access.
"""

from __future__ import annotations

import json
import re
from typing import Any

# ---------------------------------------------------------------------------
# Bounded influence
# ---------------------------------------------------------------------------

# The deterministic 0-100 score is the primary score. DeepSeek may move it by
# at most this many points in either direction. 86 -> 94 is possible;
# 86 -> 40 is not.
MAX_ADJUSTMENT = 8.0

# A REJECT verdict must actually hurt, and an APPLY verdict must actually help,
# but both stay inside MAX_ADJUSTMENT.
_REJECT_CEILING = -4.0
_APPLY_FLOOR = 2.0

# ---------------------------------------------------------------------------
# Strict output schema
# ---------------------------------------------------------------------------

RECOMMENDATIONS = ("APPLY", "MAYBE", "REJECT")
INTERVIEW_LEVELS = ("NONE", "LOW", "MEDIUM", "HIGH")
BUDGET_QUALITIES = ("GOOD", "FAIR", "POOR", "UNKNOWN")
RISK_LEVELS = ("LOW", "MEDIUM", "HIGH")

REQUIRED_KEYS = (
    "llm_score",
    "recommendation",
    "task_type",
    "estimated_effort",
    "interview_likelihood",
    "budget_quality",
    "risk",
    "reason",
)

# Bounds that keep "no arbitrary prose" honest.
_MAX_LEN = {
    "task_type": 60,
    "estimated_effort": 40,
    "reason": 300,
}

SCHEMA_EXAMPLE = {
    "llm_score": 8,
    "recommendation": "APPLY",
    "task_type": "FastAPI bug fix",
    "estimated_effort": "1-2 days",
    "interview_likelihood": "LOW",
    "budget_quality": "GOOD",
    "risk": "LOW",
    "reason": "Fixed-price, concrete deliverable, matches FastAPI/PostgreSQL experience.",
}

# Compact capability summary. Deliberately shorter than the proposal fact sheet:
# screening only needs enough context to judge fit, and prompt tokens are the
# dominant cost of a screening call.
CAPABILITIES = """\
Python, FastAPI, Flask, REST APIs, PostgreSQL, SQLAlchemy, async Python.
React, Vite, TypeScript, JavaScript. SQL.
LLM APIs (OpenAI-style, DeepSeek, Groq, Llama), structured LLM extraction,
prompt engineering, RAG, embeddings, chatbots.
Web scraping (Playwright, Selenium), automation, data-processing scripts.
WhatsApp / Telegram / Gmail / Google Places / Razorpay integrations, webhooks.
Docker, AWS (Lightsail), Nginx, Cloudflare, Vercel, Linux, GitHub Actions, CI/CD.
API design, state machines, authentication, testing, security, debugging.
Shipped THRYVIX AI (production clinic OS, solo, live users) and COGEXT
(LLM extraction pipeline, state machine, HMAC webhooks, published Python SDK).
Self-taught, early-career, no degree, India-based, remote only, works async.
Will NOT sit interviews: wants fixed-price or one-time task work."""

TASK_QUESTIONS = """\
1. Is this genuinely a paid technical task (not employment, not unpaid)?
2. Is the deliverable clearly defined?
3. Is this realistically completable by this person?
4. Does it match their actual technical capabilities?
5. Is the budget reasonable for the apparent workload?
6. Is this likely to involve interviews or employment-style hiring?
7. Is there hidden long-term or ongoing work?
8. Are there scam or suspicious signals?
9. Would this be worth spending time applying to?
10. Give a concise reason for the recommendation."""


class InvalidScreenResult(ValueError):
    """Raised when the model's response does not match the required schema.

    Treated by the caller as a failed call: the deterministic score stands."""


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def build_screen_prompt(job: dict, evaluation: Any = None) -> str:
    """Prompt for a single candidate.

    Asks only for the qualitative judgements deterministic code cannot make.
    It is not asked to rewrite the listing or to produce prose.
    """
    description = (job.get("description") or "")[:2500]
    context = ""
    if evaluation is not None:
        score = getattr(evaluation, "opportunity_score", None)
        matched = ", ".join(getattr(evaluation, "matched_skills", [])[:8])
        hours = getattr(evaluation, "estimated_hours", 0) or 0
        budget = (
            evaluation.budget.describe()
            if getattr(evaluation, "budget", None)
            else "not stated"
        )
        context = (
            f"\nDETERMINISTIC PRESCREEN (already computed, do not repeat):\n"
            f"score {score}/100 | budget {budget} | ~{hours:g}h | skills: {matched or 'none'}\n"
        )

    return f"""You are the final judgement layer for one engineering candidate. Deterministic
filters have already accepted this listing; judge only the qualitative questions
below. Be skeptical: rejecting a bad task is more valuable than approving a
mediocre one.

CANDIDATE:
{CAPABILITIES}

LISTING:
Source: {job.get('source', '')}
Title: {job.get('title', '')}
Company: {job.get('company', '')}
Work type: {job.get('work_type', '')}
Location: {job.get('location', '')}
Stated budget: {job.get('salary', '') or 'not stated'}
Competition: {job.get('competition') if job.get('competition') is not None else 'unknown'}
{context}
Description:
{description}

ANSWER THESE QUESTIONS:
{TASK_QUESTIONS}

Scoring guide for llm_score (1-10):
  9-10  Clearly paid, well-defined, small, squarely within the candidate's stack.
  7-8   Good paid task with a minor gap.
  5-6   Plausible but with a real doubt (vague scope, weak budget, unclear fit).
  3-4   Probably not worth applying to.
  1-2   Not a paid technical task, or a scam, or clearly out of reach.

Reply with ONLY this JSON object and nothing else. No prose, no markdown, no
extra keys. Every field is required.
{json.dumps(SCHEMA_EXAMPLE, indent=2)}

Rules:
- "llm_score" must be an integer 1-10.
- "recommendation" must be exactly one of {list(RECOMMENDATIONS)}.
- "interview_likelihood" must be exactly one of {list(INTERVIEW_LEVELS)}.
- "budget_quality" must be exactly one of {list(BUDGET_QUALITIES)}.
- "risk" must be exactly one of {list(RISK_LEVELS)}.
- "task_type" is a short label, at most {_MAX_LEN['task_type']} characters.
- "estimated_effort" is a short phrase, at most {_MAX_LEN['estimated_effort']} characters.
- "reason" is one sentence, at most {_MAX_LEN['reason']} characters."""


# ---------------------------------------------------------------------------
# Strict validation
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _coerce_object(payload: str | dict) -> dict:
    if isinstance(payload, dict):
        return payload
    text = _FENCE_RE.sub("", str(payload or "")).strip()
    if not text:
        raise InvalidScreenResult("empty response")
    try:
        parsed = json.loads(text)
    except ValueError:
        match = _OBJECT_RE.search(text)
        if not match:
            raise InvalidScreenResult(f"no JSON object in response: {text[:120]!r}")
        try:
            parsed = json.loads(match.group(0))
        except ValueError as exc:
            raise InvalidScreenResult(f"malformed JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise InvalidScreenResult(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def _enum(value: Any, allowed: tuple[str, ...], field: str) -> str:
    text = str(value or "").strip().upper()
    if text not in allowed:
        raise InvalidScreenResult(
            f"{field} must be one of {list(allowed)}, got {value!r}"
        )
    return text


def _short_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise InvalidScreenResult(f"{field} must be a non-empty string")
    limit = _MAX_LEN[field]
    if len(text) > limit:
        raise InvalidScreenResult(f"{field} is {len(text)} chars, limit is {limit}")
    if "\n" in text:
        raise InvalidScreenResult(f"{field} must be a single line")
    return text


def parse_screen_result(payload: str | dict) -> dict:
    """Validate the model's response against the required schema.

    Raises :class:`InvalidScreenResult` for anything that does not conform, so
    the caller can fall back to the deterministic score rather than trusting
    half-parsed output."""
    data = _coerce_object(payload)

    missing = [key for key in REQUIRED_KEYS if key not in data]
    if missing:
        raise InvalidScreenResult(f"missing keys: {missing}")

    try:
        score = int(data["llm_score"])
    except (TypeError, ValueError) as exc:
        raise InvalidScreenResult(f"llm_score must be an integer, got {data['llm_score']!r}") from exc
    if not 1 <= score <= 10:
        raise InvalidScreenResult(f"llm_score out of range: {score}")

    return {
        "llm_score": score,
        "recommendation": _enum(data["recommendation"], RECOMMENDATIONS, "recommendation"),
        "task_type": _short_text(data["task_type"], "task_type"),
        "estimated_effort": _short_text(data["estimated_effort"], "estimated_effort"),
        "interview_likelihood": _enum(
            data["interview_likelihood"], INTERVIEW_LEVELS, "interview_likelihood"
        ),
        "budget_quality": _enum(data["budget_quality"], BUDGET_QUALITIES, "budget_quality"),
        "risk": _enum(data["risk"], RISK_LEVELS, "risk"),
        "reason": _short_text(data["reason"], "reason"),
    }


# ---------------------------------------------------------------------------
# Bounded adjustment
# ---------------------------------------------------------------------------


def score_adjustment(analysis: dict | None) -> float:
    """Translate a validated analysis into a bounded score delta.

    Uses ``llm_score``, then lets ``recommendation`` enforce a floor or ceiling.
    The result is always within +/- MAX_ADJUSTMENT. Anything unvalidated or
    unparseable contributes nothing at all."""
    if not analysis:
        return 0.0

    raw = analysis.get("llm_score", analysis.get("score"))
    try:
        score = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if not 1 <= score <= 10:
        return 0.0

    # 5.5 is the neutral midpoint, so 1 -> -MAX and 10 -> +MAX.
    delta = (score - 5.5) / 4.5 * MAX_ADJUSTMENT

    verdict = str(analysis.get("recommendation") or "").strip().upper()
    if verdict == "REJECT":
        delta = min(delta, _REJECT_CEILING)
    elif verdict == "APPLY":
        delta = max(delta, _APPLY_FLOOR)

    return max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, delta))


# ---------------------------------------------------------------------------
# Caching key
# ---------------------------------------------------------------------------


def content_hash(job: dict) -> str:
    """Fingerprint of the parts of a listing that could change the judgement.

    Cached analyses are reused while this is unchanged, so the same listing is
    never sent to DeepSeek twice unless it materially changed."""
    import hashlib

    material = "\n".join(
        [
            str(job.get("title") or "").strip().lower(),
            str(job.get("company") or "").strip().lower(),
            str(job.get("salary") or "").strip().lower(),
            str(job.get("work_type") or "").strip().lower(),
            re.sub(r"\s+", " ", str(job.get("description") or "")).strip().lower(),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
