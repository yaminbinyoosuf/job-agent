"""Proposal drafting for shortlisted leads.

Two rules shape this module:

1. **Reference the actual task.** A proposal that could have been sent to any
   listing is worse than no proposal. The prompt is fed the real posting text
   and the extracted deliverable signals.
2. **Never claim unsupported experience.** The model is given a closed fact
   sheet and told it may only draw on that.

Generation takes an injected ``llm_call`` callable so this module has no
network dependency of its own and stays unit-testable.
"""

from __future__ import annotations

from typing import Callable

# Closed set of things the candidate has actually done. The proposal may not
# go beyond this, and the fallback template is built from it directly.
FACT_SHEET: tuple[str, ...] = (
    "Built and deployed THRYVIX AI, a production clinic operating system, solo: "
    "FastAPI + SQLAlchemy + PostgreSQL backend, React/Vite front end.",
    "Deployed it to production on AWS Lightsail with Docker, Nginx, Cloudflare and SSL.",
    "Integrated the WhatsApp Cloud API for automated English and Malayalam messaging.",
    "Integrated Sarvam AI for Malayalam voice-based booking.",
    "Built COGEXT: an LLM extraction pipeline (Groq / Llama 3.3 70B) producing "
    "structured JSON with validation, retries and confidence scoring.",
    "Designed a 12-state commitment lifecycle state machine with atomic database "
    "transitions and an append-only event history.",
    "Implemented webhooks with HMAC-SHA256 verification and SSRF protection.",
    "Built automated scraping and outreach with Python, Playwright, the Google "
    "Places API, Gmail, Google Sheets and GitHub Actions.",
    "Published a Python SDK with 20+ API methods and automated tests.",
    "Works with OpenAI-style LLM APIs, DeepSeek and Groq; does prompt engineering "
    "and structured extraction.",
    "Comfortable with REST API design, authentication, testing, CI/CD and Linux "
    "server operations.",
)

DISALLOWED_CLAIMS: tuple[str, ...] = (
    "years of enterprise experience",
    "led a team",
    "managed engineers",
    "worked at a FAANG",
    "large-scale distributed systems at scale",
    "maintained legacy systems for a decade",
    "extensive experience with Java/C#/Go/Rust at scale",
    "mobile development experience",
    "data science PhD",
)

PROPOSAL_SYSTEM = (
    "You write short, specific freelance proposals for one engineer. You never "
    "invent experience, never use marketing filler, and never write anything that "
    "could be sent to a different listing unchanged."
)


def build_proposal_prompt(job: dict, evaluation, llm_result: dict | None = None) -> str:
    """Prompt for a grounded, task-specific proposal."""
    facts = "\n".join(f"- {fact}" for fact in FACT_SHEET)
    matched = ", ".join(evaluation.matched_skills[:10]) or "none detected"
    clarity = ", ".join(evaluation.clarity_hits[:8]) or "none detected"
    budget = evaluation.budget.describe() if evaluation.budget else "not stated"
    reason = (llm_result or {}).get("reason") or ""
    description = (job.get("description") or "")[:4000]

    return f"""Write a freelance proposal for the task below, on behalf of Yamin Binyoosuf.

TASK:
Title: {job.get('title', '')}
Platform: {job.get('source', '')}
Client: {job.get('company', '')}
Budget: {budget}
Estimated effort: about {evaluation.estimated_hours:g} hours
Work type: {job.get('work_type', '')}
Description:
{description}

WHAT THE SCREENING FOUND (context, do not repeat verbatim):
Matched skills: {matched}
Deliverable signals: {clarity}
Screening note: {reason}

EXPERIENCE YOU MAY DRAW ON — this is the complete list. Do not go beyond it:
{facts}

FORBIDDEN CLAIMS (never write these or anything like them):
{'; '.join(DISALLOWED_CLAIMS)}

Write the proposal with exactly this shape, no headings, no markdown, plain text:
1. One or two sentences confirming you understand the specific task.
2. One or two sentences of directly relevant experience from the list above.
3. A brief, concrete implementation approach for THIS task.
4. A realistic delivery expectation (use the estimated effort above).
5. At most one or two genuinely necessary clarifying questions, or omit them.

Keep it under 160 words. Write as Yamin, first person. No greetings like
"Dear Sir/Madam" and no sign-off block — the platform supplies contact details.

Respond with ONLY a JSON object:
{{"proposal": "<the proposal text, plain text with real newlines>"}}"""


def fallback_proposal(job: dict, evaluation) -> str:
    """Deterministic proposal used when no model is available.

    Built only from the fact sheet and the listing, so it can never fabricate.
    """
    title = (job.get("title") or "this task").strip()
    hours = evaluation.estimated_hours or 0
    if hours <= 8:
        delivery = "I can start immediately and deliver within a day."
    elif hours <= 24:
        delivery = "I can start immediately and deliver within 1-2 days."
    elif hours <= 48:
        delivery = "I can start immediately and deliver within 3-5 days."
    else:
        delivery = "I can start immediately and keep you updated at each milestone."

    skills = ", ".join(evaluation.matched_skills[:4])
    if skills:
        relevance = (
            f"I've built and deployed production systems using {skills} — including "
            "a live clinic platform (FastAPI + PostgreSQL + React on AWS) and an "
            "LLM extraction pipeline with structured JSON output."
        )
    else:
        relevance = (
            "I've built and deployed production Python/FastAPI systems end to end, "
            "including a live clinic platform on AWS and an LLM extraction pipeline."
        )

    approach = (
        f"For \"{title}\" I'd start by reproducing the current behaviour, isolate "
        "the exact point that needs changing, implement the fix or integration, "
        "then verify the affected flow before handing it over."
    )

    return f"{relevance}\n\n{approach}\n\n{delivery}"


def generate_proposal(
    job: dict,
    evaluation,
    llm_result: dict | None,
    llm_call: Callable[[str], dict] | None = None,
) -> tuple[str, str]:
    """Return ``(proposal_text, origin)`` where origin is 'model' or 'fallback'."""
    if llm_call is not None:
        try:
            result = llm_call(build_proposal_prompt(job, evaluation, llm_result))
            text = str((result or {}).get("proposal") or "").strip()
            if text:
                if _contains_forbidden_claim(text):
                    return fallback_proposal(job, evaluation), "fallback"
                return text, "model"
        except Exception:  # noqa: BLE001 - a proposal is never worth failing a run
            pass
    return fallback_proposal(job, evaluation), "fallback"


def _contains_forbidden_claim(text: str) -> bool:
    low = text.lower()
    return any(claim in low for claim in DISALLOWED_CLAIMS)
