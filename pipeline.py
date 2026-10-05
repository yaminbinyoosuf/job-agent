"""The screening funnel: raw listings in, a handful of winnable tasks out.

    RAW LISTINGS
      -> deduplicate
      -> hard rejection filters        (cheap, no model)
      -> preliminary score
      -> skill match / payment / etc.  (model-assisted, budgeted)
      -> opportunity score
      -> quality threshold
      -> top N leads

The model is called *after* the cheap deterministic filters and only for the
best-scoring candidates, which keeps cost proportional to promise rather than
to raw listing volume.

This module owns the funnel so that ``job_agent.main`` and the tests exercise
exactly the same code path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import leadscore

log = logging.getLogger("taskagent.pipeline")


@dataclass
class ScreenedLead:
    job: dict
    evaluation: leadscore.Evaluation
    llm_result: dict | None = None

    @property
    def score(self) -> int:
        return self.evaluation.opportunity_score

    @property
    def lead_id(self) -> str:
        return str(self.job.get("id"))


@dataclass
class ScreeningResult:
    raw_count: int = 0
    deduped_count: int = 0
    rejected: list[tuple[dict, list[str]]] = field(default_factory=list)
    leads: list[ScreenedLead] = field(default_factory=list)
    # Everything the model actually scored, including those below the bar, so
    # the audit log can record the full decision.
    scored_leads: list[ScreenedLead] = field(default_factory=list)
    scored_count: int = 0
    llm_calls: int = 0
    below_threshold: int = 0

    @property
    def rejection_summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _job, reasons in self.rejected:
            for reason in reasons:
                counts[reason] = counts.get(reason, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def stats(self) -> dict[str, Any]:
        return {
            "raw": self.raw_count,
            "deduped": self.deduped_count,
            "rejected": len(self.rejected),
            "scored": self.scored_count,
            "below_threshold": self.below_threshold,
            "leads": len(self.leads),
            "llm_calls": self.llm_calls,
            "rejection_summary": self.rejection_summary,
        }


def _default_dedupe(jobs: list[dict]) -> list[dict]:
    """Reuse the existing de-duplication from job_agent (imported lazily to
    avoid a circular import at module load)."""
    from job_agent import dedupe

    return dedupe(jobs)


def screen_jobs(
    jobs: list[dict],
    *,
    now: datetime | None = None,
    min_score: int = 60,
    limit: int = 8,
    min_hourly_value: float = 8.0,
    require_budget: bool = False,
    llm_call: Callable[[dict], dict] | None = None,
    llm_budget: int = 40,
    known_ids: set[str] | None = None,
    prefilter: Callable[[dict], str | None] | None = None,
    dedupe_fn: Callable[[list[dict]], list[dict]] | None = None,
    store: Any = None,
) -> ScreeningResult:
    """Run the full funnel.

    ``llm_call`` is injected so this stays testable and so the caller decides
    which model to spend. When it is None the funnel still works, using only
    deterministic signals.

    ``known_ids`` are listings already handled on a previous run. They are
    dropped before the model is consulted, which both saves money and makes it
    impossible to re-email the same opportunity.

    ``prefilter`` returns a rejection reason for listings that should never
    reach scoring. The agent passes its existing title blocklist and keyword
    evidence rules through this seam, so that logic keeps working without this
    module importing the agent.
    """
    result = ScreeningResult(raw_count=len(jobs))

    dedupe_fn = dedupe_fn or _default_dedupe
    unique = dedupe_fn(jobs)
    result.deduped_count = len(unique)
    log.info("dedupe: %d raw -> %d unique", len(jobs), len(unique))

    if known_ids:
        before = len(unique)
        unique = [job for job in unique if str(job.get("id")) not in known_ids]
        log.info("already_handled: %d filtered, %d new", before - len(unique), len(unique))

    # 1. Cheap deterministic triage, including the full hard-rejection set.
    survivors: list[tuple[dict, leadscore.Evaluation]] = []
    for job in unique:
        # Cheap title/keyword gate first, supplied by the caller.
        if prefilter is not None:
            reason = prefilter(job)
            if reason:
                result.rejected.append((job, [reason]))
                continue
        rejection = leadscore.rejection_reasons(
            job,
            now=now,
            min_hourly_value=min_hourly_value,
            require_budget=require_budget,
        )
        if rejection:
            result.rejected.append((job, rejection))
            log.debug(
                "rejected id=%s reasons=%s title=%r",
                job.get("id"), ",".join(rejection), (job.get("title") or "")[:60],
            )
            continue
        evaluation = leadscore.evaluate(
            job, None, now=now, min_hourly_value=min_hourly_value, require_budget=require_budget
        )
        survivors.append((job, evaluation))

    log.info(
        "hard filters: %d survived, %d rejected (%s)",
        len(survivors),
        len(result.rejected),
        result.rejection_summary,
    )

    # 2. Spend the model only on the most promising candidates.
    survivors.sort(key=lambda pair: pair[1].opportunity_score, reverse=True)
    to_score = survivors[: max(0, llm_budget)]
    untouched = survivors[len(to_score):]

    scored: list[ScreenedLead] = []
    for job, base_evaluation in to_score:
        llm_result = None
        if llm_call is not None:
            try:
                llm_result = llm_call(job)
                result.llm_calls += 1
            except Exception as exc:  # noqa: BLE001 - one bad call must not stop the run
                log.warning("llm screening failed for %s: %s", job.get("id"), exc)
        evaluation = leadscore.evaluate(
            job,
            llm_result,
            now=now,
            min_hourly_value=min_hourly_value,
            require_budget=require_budget,
        )
        scored.append(ScreenedLead(job, evaluation, llm_result))

    result.scored_count = len(scored)
    result.scored_leads = list(scored)

    # 3. Quality bar. Deliberately not lowered to fill the list.
    qualified = [lead for lead in scored if lead.score >= min_score]
    result.below_threshold = len(scored) - len(qualified)
    for job, evaluation in untouched:
        result.below_threshold += 1

    qualified.sort(key=lambda lead: lead.score, reverse=True)
    result.leads = qualified[:limit]

    log.info(
        "funnel: %d raw -> %d unique -> %d survived filters -> %d scored -> "
        "%d at/above %d -> showing %d",
        result.raw_count,
        result.deduped_count,
        len(survivors),
        result.scored_count,
        len(qualified),
        min_score,
        len(result.leads),
    )

    if store is not None:
        persist(result, store)
    return result


def persist(result: ScreeningResult, store: Any) -> None:
    """Write leads into the store without duplicating existing ones."""
    for lead in result.leads:
        evaluation = lead.evaluation
        store.upsert_lead(
            {
                "lead_id": lead.lead_id,
                "source": lead.job.get("source", ""),
                "title": lead.job.get("title", ""),
                "company": lead.job.get("company", ""),
                "url": lead.job.get("url", ""),
                "work_type": lead.job.get("work_type", ""),
                "opportunity_score": lead.score,
                "priority": evaluation.priority,
                "skill_match_pct": evaluation.skill_match_pct,
                "interview": evaluation.interview,
                "estimated_hours": evaluation.estimated_hours,
                "implied_hourly_usd": evaluation.implied_hourly_usd,
                "budget_text": evaluation.budget.describe() if evaluation.budget else "not stated",
                "budget_usd": evaluation.expected_value_usd,
                "competition": lead.job.get("competition"),
                "location": lead.job.get("location", ""),
                "posted_at": lead.job.get("posted_at"),
                "why": _why(evaluation),
                "reason": (lead.llm_result or {}).get("reason", ""),
                "red_flags": evaluation.red_flags,
                "matched_skills": evaluation.matched_skills,
            }
        )


def _why(evaluation: leadscore.Evaluation) -> str:
    """A one-line justification built from the actual component scores."""
    parts: list[str] = []
    if evaluation.matched_skills:
        parts.append("matches " + ", ".join(evaluation.matched_skills[:4]))
    if evaluation.budget and evaluation.budget.is_known:
        parts.append(
            f"{evaluation.budget.describe()} for ~{evaluation.estimated_hours:g}h"
        )
    if evaluation.estimated_hours <= 16:
        parts.append("short scope")
    if evaluation.interview in ("NONE", "LOW"):
        parts.append(f"{evaluation.interview.lower()} interview risk")
    if not parts:
        return "no strong signals detected"
    return "; ".join(parts).capitalize() + "."
