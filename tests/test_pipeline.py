"""End-to-end funnel tests: raw listings in, a small number of tasks out.

These exercise ``pipeline.screen_jobs`` — the same function the scheduled agent
calls — so the funnel shape asserted here is the real behaviour, not a copy.
No network and no model calls are involved.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard  # noqa: E402
import leadscore as ls  # noqa: E402
import leadstore  # noqa: E402
import pipeline  # noqa: E402
import proposals  # noqa: E402

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Corpus builders
# ---------------------------------------------------------------------------


def good_task(index: int, **overrides) -> dict:
    base = {
        "id": f"freelancer.com:good{index}",
        "source": "Freelancer.com",
        "title": f"Fix FastAPI {index} endpoint returning 500 on upload",
        "company": f"client{index}",
        "url": f"https://example.test/good/{index}",
        "work_type": "task",
        "no_interview": True,
        "competition": 3,
        "posted_at": NOW - timedelta(hours=3),
        "tags": ["Python", "FastAPI"],
        "salary": "USD 250-400 fixed",
        "description": (
            "FastAPI + PostgreSQL application. The upload endpoint returns 500 on "
            "files over 10MB. Fix the bug, add a regression test, and confirm the "
            "flow works. Small fixed-price task with clear acceptance criteria. "
            "Acceptance criteria: large uploads succeed and existing tests pass."
        ),
    }
    base.update(overrides)
    return base


def full_time_job(index: int) -> dict:
    return {
        "id": f"remoteok:ft{index}",
        "source": "RemoteOK",
        "title": f"Senior AI Engineer {index}",
        "company": f"BigCo{index}",
        "url": f"https://example.test/ft/{index}",
        "work_type": "full_time",
        "no_interview": None,
        "competition": None,
        "posted_at": NOW - timedelta(hours=1),
        "tags": [],
        "salary": "$180000/year",
        "description": (
            "Full-time permanent position. Our interview process has multiple "
            "rounds including a whiteboard and a panel. Requires 8+ years and "
            "long-term commitment."
        ),
    }


def junk_task(index: int) -> dict:
    return {
        "id": f"freelancer.com:junk{index}",
        "source": "Freelancer.com",
        "title": f"Data entry and copywriting project {index}",
        "company": f"client{index}",
        "url": f"https://example.test/junk/{index}",
        "work_type": "task",
        "no_interview": True,
        "competition": 90,
        "posted_at": NOW - timedelta(days=20),
        "tags": [],
        "salary": "USD 20-30 fixed",
        "description": "Enter data into a spreadsheet. Ongoing long-term work, various tasks.",
    }


def scam_task(index: int) -> dict:
    return {
        "id": f"freelancer.com:scam{index}",
        "source": "Freelancer.com",
        "title": f"Python script urgent {index}",
        "company": f"client{index}",
        "url": f"https://example.test/scam/{index}",
        "work_type": "task",
        "no_interview": True,
        "competition": 1,
        "posted_at": NOW,
        "tags": ["Python"],
        "salary": "USD 500 fixed",
        "description": "Simple Python script. Please pay a registration fee to start work.",
    }


def build_corpus() -> list[dict]:
    corpus: list[dict] = []
    corpus += [good_task(i) for i in range(100)]
    corpus += [good_task(i) for i in range(50)]          # exact duplicates
    corpus += [full_time_job(i) for i in range(200)]
    corpus += [junk_task(i) for i in range(120)]
    corpus += [scam_task(i) for i in range(30)]
    return corpus


def fake_llm(job: dict) -> dict:
    """Stand-in for the DeepSeek screen: endorses real tasks, dismisses junk."""
    text = f"{job.get('title','')} {job.get('description','')}".lower()
    if "fastapi" in text and "bug" in text or "endpoint" in text:
        return {"score": 9, "reason": "Direct FastAPI/PostgreSQL match.",
                "interview_process": "none", "work_type": "task"}
    return {"score": 2, "reason": "Weak match.", "interview_process": "standard",
            "work_type": "full_time"}


class TestFunnelShape(unittest.TestCase):
    def setUp(self):
        self.corpus = build_corpus()
        self.result = pipeline.screen_jobs(
            self.corpus, now=NOW, min_score=60, limit=8, llm_call=fake_llm, llm_budget=40
        )

    def test_reads_the_whole_corpus(self):
        self.assertEqual(self.result.raw_count, len(self.corpus))
        self.assertGreater(self.result.raw_count, 400)

    def test_dedupe_collapses_repeats(self):
        self.assertLess(self.result.deduped_count, self.result.raw_count)
        self.assertGreaterEqual(self.result.deduped_count, 100)

    def test_produces_a_small_number_of_leads(self):
        self.assertGreaterEqual(len(self.result.leads), 1)
        self.assertLessEqual(len(self.result.leads), 8, "must respect the lead cap")

    def test_every_lead_clears_the_quality_bar(self):
        for lead in self.result.leads:
            self.assertGreaterEqual(lead.score, 60, lead.job["title"])

    def test_leads_are_sorted_by_score(self):
        scores = [lead.score for lead in self.result.leads]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_lead_ids_are_unique(self):
        ids = [lead.lead_id for lead in self.result.leads]
        self.assertEqual(len(ids), len(set(ids)))

    def test_no_full_time_work_survives(self):
        for lead in self.result.leads:
            self.assertNotEqual(lead.job["work_type"], "full_time")

    def test_llm_budget_is_respected(self):
        self.assertLessEqual(self.result.llm_calls, 40)

    def test_funnel_actually_narrows(self):
        """The whole point: hundreds in, a handful out."""
        self.assertGreater(self.result.deduped_count, 10 * len(self.result.leads))
        self.assertLess(len(self.result.leads), self.result.deduped_count / 10)

    def test_rejection_reasons_are_reported(self):
        summary = self.result.rejection_summary
        self.assertGreater(summary.get("full_time", 0), 0)
        self.assertGreater(summary.get("scam", 0), 0)

    def test_stats_serialisable(self):
        import json

        json.dumps(self.result.stats())


class TestHardFiltering(unittest.TestCase):
    def test_full_time_is_rejected(self):
        result = pipeline.screen_jobs([full_time_job(1)], now=NOW, min_score=60, limit=8)
        self.assertEqual(result.leads, [])
        self.assertIn("full_time", result.rejected[0][1])

    def test_scam_is_rejected_even_when_well_paid(self):
        result = pipeline.screen_jobs([scam_task(1)], now=NOW, min_score=60, limit=8)
        self.assertEqual(result.leads, [])
        self.assertIn("scam", result.rejected[0][1])

    def test_low_budget_high_bids_rejected(self):
        result = pipeline.screen_jobs([junk_task(1)], now=NOW, min_score=60, limit=8)
        self.assertEqual(result.leads, [])

    def test_good_task_survives(self):
        result = pipeline.screen_jobs([good_task(1)], now=NOW, min_score=60, limit=8)
        self.assertEqual(len(result.leads), 1)

    def test_llm_failure_does_not_stop_the_run(self):
        def exploding(_job):
            raise RuntimeError("model unavailable")

        result = pipeline.screen_jobs(
            [good_task(1), good_task(2)], now=NOW, min_score=60, limit=8, llm_call=exploding
        )
        # Deterministic signals alone must still produce leads.
        self.assertGreaterEqual(len(result.leads), 1)

    def test_works_without_any_llm(self):
        result = pipeline.screen_jobs([good_task(1)], now=NOW, min_score=60, limit=8)
        self.assertEqual(len(result.leads), 1)
        self.assertEqual(result.llm_calls, 0)


class TestQualityOverQuantity(unittest.TestCase):
    def test_does_not_pad_the_list(self):
        """Three good tasks in a sea of junk must yield three leads, not eight."""
        corpus = [good_task(i) for i in range(3)] + [junk_task(i) for i in range(50)]
        result = pipeline.screen_jobs(corpus, now=NOW, min_score=60, limit=8)
        self.assertEqual(len(result.leads), 3)

    def test_threshold_is_not_lowered(self):
        result = pipeline.screen_jobs([good_task(1)], now=NOW, min_score=99, limit=8)
        self.assertEqual(result.leads, [])


class TestPersistenceAndOutput(unittest.TestCase):
    def test_leads_persist_and_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            with leadstore.LeadStore(Path(tmp) / "leads.db") as store:
                result = pipeline.screen_jobs(
                    build_corpus(), now=NOW, min_score=60, limit=8,
                    llm_call=fake_llm, llm_budget=40, store=store,
                )
                rows = store.top_leads(limit=10)
                self.assertEqual(len(rows), len(result.leads))

                # Re-running must not duplicate anything.
                pipeline.screen_jobs(
                    build_corpus(), now=NOW, min_score=60, limit=8,
                    llm_call=fake_llm, llm_budget=40, store=store,
                )
                self.assertEqual(len(store.top_leads(limit=50)), len(result.leads))

                text = dashboard.render_digest(rows, now=NOW)
                html = dashboard.render_dashboard_html(rows, now=NOW)
                self.assertIn("TODAY'S PAID TASK LEADS", text)
                self.assertIn("Today's Best Paid Tasks", html)
                for row in rows:
                    self.assertIn(row["title"][:20], html)

    def test_proposal_generated_for_a_stored_lead(self):
        with tempfile.TemporaryDirectory() as tmp:
            with leadstore.LeadStore(Path(tmp) / "leads.db") as store:
                result = pipeline.screen_jobs(
                    [good_task(1)], now=NOW, min_score=60, limit=8, store=store
                )
                lead = result.leads[0]
                text, origin = proposals.generate_proposal(
                    lead.job, lead.evaluation, lead.llm_result, llm_call=None
                )
                self.assertEqual(origin, "fallback")
                self.assertIn("FastAPI", text)
                store.set_proposal(lead.lead_id, text)
                self.assertEqual(store.get_lead(lead.lead_id)["proposal"], text)


class TestFunnelMetrics(unittest.TestCase):
    def test_end_to_end_funnel_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            with leadstore.LeadStore(Path(tmp) / "leads.db") as store:
                result = pipeline.screen_jobs(
                    build_corpus(), now=NOW, min_score=60, limit=8,
                    llm_call=fake_llm, llm_budget=40, store=store,
                )
                self.assertEqual(store.funnel_metrics(days=30).discovered, len(result.leads))

                lead_id = result.leads[0].lead_id
                store.set_state(lead_id, "QUALIFIED")
                store.set_state(lead_id, "APPLIED")
                store.set_state(lead_id, "REPLIED")
                store.set_state(lead_id, "WON")
                store.set_state(lead_id, "COMPLETED")
                store.set_state(lead_id, "PAID")
                store.record_revenue(lead_id, 300, "USD", "collected")

                metrics = store.funnel_metrics(days=30)
                self.assertEqual(metrics.applied, 1)
                self.assertEqual(metrics.paid, 1)
                revenue = store.revenue_summary(debt_target_inr=418000)
                self.assertEqual(revenue.collected_usd, 300)
                self.assertGreater(revenue.debt_reduction_pct, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
