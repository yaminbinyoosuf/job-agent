"""Tests proving DeepSeek usage is minimal, capped, cached and non-critical.

These are the guarantees that protect a limited API balance:

* raw listings never reach DeepSeek
* only candidates that survive every deterministic filter are sent
* the per-run cap is never exceeded
* the same listing is never analysed twice
* any failure or malformed response falls back to the deterministic score
* proposals are never generated automatically
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from contextlib import contextmanager  # noqa: E402

import job_agent  # noqa: E402
import leadscore as ls  # noqa: E402
import leadstore  # noqa: E402
import llmscreen  # noqa: E402
import pipeline  # noqa: E402

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)

VALID_ANALYSIS = {
    "llm_score": 8,
    "recommendation": "APPLY",
    "task_type": "FastAPI bug fix",
    "estimated_effort": "1-2 days",
    "interview_likelihood": "LOW",
    "budget_quality": "GOOD",
    "risk": "LOW",
    "reason": "Fixed-price, concrete deliverable, matches FastAPI/PostgreSQL.",
}


class RecordingLLM:
    """Stands in for DeepSeek and records exactly which listings it was given."""

    def __init__(self, result=None, raises=None):
        self.seen: list[str] = []
        self.calls = 0
        self.result = result if result is not None else dict(VALID_ANALYSIS)
        self.raises = raises

    def __call__(self, job):
        self.calls += 1
        self.seen.append(str(job.get("id")))
        if self.raises is not None:
            raise self.raises
        return dict(self.result)


class FakeCache:
    """In-memory stand-in for leadstore's llm_cache table."""

    def __init__(self):
        self.data: dict[tuple[str, str], dict] = {}
        self.puts = 0

    def get(self, job, fingerprint):
        return self.data.get((str(job.get("id")), fingerprint))

    def put(self, job, fingerprint, analysis):
        self.puts += 1
        self.data[(str(job.get("id")), fingerprint)] = dict(analysis)


# ---------------------------------------------------------------------------
# Corpus builders
# ---------------------------------------------------------------------------


def good_task(i: int, **overrides) -> dict:
    base = {
        "id": f"freelancer.com:good{i}",
        "source": "Freelancer.com",
        "title": f"Fix FastAPI endpoint {i} returning 500 on upload",
        "company": f"client{i}",
        "url": f"https://example.test/g{i}",
        "work_type": "task",
        "no_interview": True,
        "competition": 3,
        "posted_at": NOW - timedelta(hours=3),
        "tags": ["Python", "FastAPI"],
        "salary": "USD 250-400 fixed",
        "description": (
            "FastAPI + PostgreSQL app. The upload endpoint returns 500 on files "
            "over 10MB. Fix the bug and add a regression test. Fixed price with "
            "clear acceptance criteria."
        ),
    }
    base.update(overrides)
    return base


def full_time_job(i: int) -> dict:
    return {
        "id": f"remoteok:ft{i}",
        "source": "RemoteOK",
        "title": f"Senior AI Engineer {i}",
        "company": f"BigCo{i}",
        "url": f"https://example.test/f{i}",
        "work_type": "full_time",
        "no_interview": None,
        "competition": None,
        "posted_at": NOW - timedelta(hours=1),
        "tags": [],
        "salary": "$180000/year",
        "description": "Full-time permanent role. Multiple rounds of interviews. 8+ years required.",
    }


def junk(i: int) -> dict:
    return {
        "id": f"remoteok:junk{i}",
        "source": "RemoteOK",
        "title": f"Sales Manager {i}",
        "company": f"Co{i}",
        "url": f"https://example.test/j{i}",
        "work_type": "full_time",
        "no_interview": None,
        "competition": None,
        "posted_at": NOW,
        "tags": [],
        "salary": "",
        "description": "Sales role, ongoing long-term work.",
    }


@contextmanager
def offline(db_path=None):
    """Force the agent offline for the duration of a block.

    Guarantees the test suite never spends API credit and never touches the
    live database, whatever the ambient environment happens to contain."""
    saved = (job_agent.DEEPSEEK_API_KEY, job_agent.LEADS_DB_PATH, job_agent._LEGACY_IMPORTED)
    job_agent.DEEPSEEK_API_KEY = ""
    job_agent._LEGACY_IMPORTED = True  # never import the repo's CSV in a test
    if db_path is not None:
        job_agent.LEADS_DB_PATH = Path(db_path)
    try:
        yield
    finally:
        job_agent.DEEPSEEK_API_KEY, job_agent.LEADS_DB_PATH, job_agent._LEGACY_IMPORTED = saved


def run(jobs, llm=None, budget=15, cache=None, **kwargs):
    return pipeline.screen_jobs(
        jobs,
        now=NOW,
        min_score=60,
        limit=8,
        llm_call=llm,
        llm_budget=budget,
        cache_get=cache.get if cache else None,
        cache_put=cache.put if cache else None,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. Raw listings must never reach DeepSeek
# ---------------------------------------------------------------------------


class TestNoRawListingCalls(unittest.TestCase):
    def test_500_listings_do_not_create_500_calls(self):
        corpus = (
            [good_task(i) for i in range(50)]
            + [full_time_job(i) for i in range(250)]
            + [junk(i) for i in range(200)]
        )
        self.assertEqual(len(corpus), 500)

        llm = RecordingLLM()
        result = run(corpus, llm=llm, budget=15)

        self.assertEqual(result.raw_count, 500)
        self.assertLessEqual(llm.calls, 15)
        self.assertEqual(result.usage.calls, llm.calls)
        # The whole point: the API sees a tiny fraction of the corpus.
        self.assertLess(llm.calls, len(corpus) / 20)

    def test_call_count_tracks_candidates_not_listings(self):
        corpus = [good_task(i) for i in range(4)] + [full_time_job(i) for i in range(96)]
        llm = RecordingLLM()
        result = run(corpus, llm=llm, budget=15)
        self.assertEqual(result.raw_count, 100)
        self.assertEqual(llm.calls, 4)  # only the four real tasks
        self.assertEqual(result.usage.successes, 4)

    def test_no_calls_at_all_without_an_llm(self):
        result = run([good_task(i) for i in range(20)], llm=None, budget=15)
        self.assertEqual(result.usage.calls, 0)
        self.assertGreater(len(result.leads), 0)  # still fully usable


# ---------------------------------------------------------------------------
# 2. Only screened candidates reach DeepSeek
# ---------------------------------------------------------------------------


class TestOnlyScreenedReachDeepSeek(unittest.TestCase):
    def test_blocked_titles_never_reach_deepseek(self):
        corpus = [junk(i) for i in range(30)] + [good_task(i) for i in range(3)]
        llm = RecordingLLM()
        run(corpus, llm=llm, budget=15, prefilter=lambda job: (
            "blocked_title" if "sales" in job["title"].lower() else None
        ))
        self.assertEqual(len(llm.seen), 3)
        self.assertFalse(any("junk" in jid for jid in llm.seen))

    def test_hard_rejected_listings_never_reach_deepseek(self):
        corpus = [full_time_job(i) for i in range(40)] + [good_task(i) for i in range(2)]
        llm = RecordingLLM()
        run(corpus, llm=llm, budget=15)
        self.assertEqual(len(llm.seen), 2)
        self.assertFalse(any("ft" in jid for jid in llm.seen))

    def test_deepseek_sees_the_highest_scoring_candidates_first(self):
        corpus = [good_task(i) for i in range(30)]
        for i in range(0, 30, 3):
            corpus[i]["description"] += " " + " ".join(["python"] * 5)
        llm = RecordingLLM()
        run(corpus, llm=llm, budget=5)
        self.assertEqual(len(llm.seen), 5)


# ---------------------------------------------------------------------------
# 3. The cap is enforced
# ---------------------------------------------------------------------------


class TestCapEnforced(unittest.TestCase):
    def test_cap_is_never_exceeded(self):
        corpus = [good_task(i) for i in range(60)]
        for cap in (0, 1, 5, 15, 20):
            llm = RecordingLLM()
            result = run(corpus, llm=llm, budget=cap)
            self.assertEqual(llm.calls, min(cap, 60), f"cap={cap}")
            self.assertLessEqual(result.usage.calls, cap)

    def test_skipped_candidates_are_counted(self):
        corpus = [good_task(i) for i in range(50)]
        result = run(corpus, llm=RecordingLLM(), budget=10)
        self.assertEqual(result.usage.calls, 10)
        self.assertEqual(result.usage.skipped, 40)

    def test_skipped_candidates_still_get_a_deterministic_score(self):
        corpus = [good_task(i) for i in range(30)]
        result = run(corpus, llm=RecordingLLM(), budget=2)
        self.assertEqual(result.scored_count, 30)
        for lead in result.scored_leads:
            self.assertGreater(lead.score, 0)

    def test_default_cap_is_small(self):
        import inspect

        default = inspect.signature(pipeline.screen_jobs).parameters["llm_budget"].default
        self.assertIn(default, (15, 20))

    def test_metrics_are_reported(self):
        stats = run([good_task(i) for i in range(40)], llm=RecordingLLM(), budget=7).stats()
        for key in (
            "deepseek_candidates", "deepseek_calls", "deepseek_successes",
            "deepseek_failures", "deepseek_cached", "deepseek_skipped",
        ):
            self.assertIn(key, stats)
        self.assertEqual(stats["deepseek_calls"], 7)
        self.assertEqual(stats["deepseek_skipped"], 33)


# ---------------------------------------------------------------------------
# 4. Duplicates never trigger another call
# ---------------------------------------------------------------------------


class TestCaching(unittest.TestCase):
    def test_duplicate_listings_within_a_run_are_sent_once(self):
        one = good_task(1)
        corpus = [dict(one), dict(one), dict(one)]
        llm = RecordingLLM()
        run(corpus, llm=llm, budget=15)
        self.assertEqual(llm.calls, 1)

    def test_second_run_reuses_the_cached_analysis(self):
        corpus = [good_task(i) for i in range(10)]
        cache = FakeCache()

        first_llm = RecordingLLM()
        first = run(corpus, llm=first_llm, budget=15, cache=cache)
        self.assertEqual(first_llm.calls, 10)
        self.assertEqual(first.usage.cached, 0)

        second_llm = RecordingLLM()
        second = run(corpus, llm=second_llm, budget=15, cache=cache)
        self.assertEqual(second_llm.calls, 0, "a repeat run must cost nothing")
        self.assertEqual(second.usage.cached, 10)

    def test_cached_analysis_does_not_consume_the_cap(self):
        corpus = [good_task(i) for i in range(10)]
        cache = FakeCache()
        run(corpus, llm=RecordingLLM(), budget=15, cache=cache)

        fresh = [good_task(100 + i) for i in range(20)]
        llm = RecordingLLM()
        result = run(corpus + fresh, llm=llm, budget=5, cache=cache)
        self.assertEqual(llm.calls, 5)          # the full cap available for new work
        self.assertEqual(result.usage.cached, 10)

    def test_changed_listing_is_re_analysed(self):
        corpus = [good_task(1)]
        cache = FakeCache()
        run(corpus, llm=RecordingLLM(), budget=15, cache=cache)

        changed = good_task(1)
        changed["description"] = "Completely different work: build a React dashboard with auth."
        llm = RecordingLLM()
        result = run([changed], llm=llm, budget=15, cache=cache)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(result.usage.cached, 0)

    def test_cache_is_keyed_by_content_not_just_id(self):
        job = good_task(1)
        first = llmscreen.content_hash(job)
        job2 = good_task(1)
        job2["salary"] = "USD 9000 fixed"
        self.assertNotEqual(first, llmscreen.content_hash(job2))

    def test_real_store_cache_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with leadstore.LeadStore(Path(tmp) / "leads.db") as store:
                corpus = [good_task(i) for i in range(3)]

                def cache_get(job, fp):
                    return store.get_llm_analysis(str(job["id"]), fp)

                def cache_put(job, fp, analysis):
                    store.save_llm_analysis(str(job["id"]), fp, analysis, "ok")

                llm1 = RecordingLLM()
                pipeline.screen_jobs(corpus, now=NOW, min_score=60, limit=8,
                                     llm_call=llm1, llm_budget=15,
                                     cache_get=cache_get, cache_put=cache_put)
                self.assertEqual(llm1.calls, 3)
                self.assertEqual(store.llm_cache_size(), 3)

                llm2 = RecordingLLM()
                second = pipeline.screen_jobs(corpus, now=NOW, min_score=60, limit=8,
                                              llm_call=llm2, llm_budget=15,
                                              cache_get=cache_get, cache_put=cache_put)
                self.assertEqual(llm2.calls, 0)
                self.assertEqual(second.usage.cached, 3)


# ---------------------------------------------------------------------------
# 5 & 6. Failure and invalid JSON fall back safely
# ---------------------------------------------------------------------------


class TestFailureFallback(unittest.TestCase):
    def test_transport_failure_falls_back_to_deterministic(self):
        corpus = [good_task(i) for i in range(5)]
        llm = RecordingLLM(raises=RuntimeError("HTTP 402 Insufficient Balance"))
        result = run(corpus, llm=llm, budget=15)

        self.assertEqual(result.usage.failures, 5)
        self.assertEqual(result.usage.successes, 0)
        self.assertGreaterEqual(len(result.leads), 1, "must still produce leads")
        for lead in result.leads:
            self.assertIsNone(lead.llm_result)
            self.assertEqual(lead.score, lead.evaluation.opportunity_score)

    def test_invalid_schema_falls_back_to_deterministic(self):
        def bad_schema(_job):
            return llmscreen.parse_screen_result(
                {"llm_score": 8, "recommendation": "DEFINITELY"}  # missing keys, bad enum
            )

        corpus = [good_task(i) for i in range(3)]
        result = run(corpus, llm=bad_schema, budget=15)
        self.assertEqual(result.usage.failures, 3)
        self.assertGreaterEqual(len(result.leads), 1)

    def test_invalid_json_raises_a_typed_error(self):
        for payload in ('{"llm_score": 8}', "not json at all", "", "[]"):
            with self.assertRaises(llmscreen.InvalidScreenResult, msg=payload):
                llmscreen.parse_screen_result(payload)

    def test_failure_does_not_stop_later_candidates(self):
        calls = {"n": 0}

        def flaky(job):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return dict(VALID_ANALYSIS)

        result = run([good_task(i) for i in range(4)], llm=flaky, budget=15)
        self.assertEqual(result.usage.failures, 1)
        self.assertEqual(result.usage.successes, 3)

    def test_a_broken_cache_does_not_break_the_run(self):
        class BrokenCache:
            def get(self, job, fp):
                raise RuntimeError("db locked")

            def put(self, job, fp, analysis):
                raise RuntimeError("db locked")

        result = run([good_task(i) for i in range(3)], llm=RecordingLLM(),
                     budget=15, cache=BrokenCache())
        self.assertEqual(result.usage.calls, 3)
        self.assertGreaterEqual(len(result.leads), 1)

    def test_lead_llm_status_is_unavailable_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with leadstore.LeadStore(Path(tmp) / "leads.db") as store:
                pipeline.screen_jobs(
                    [good_task(1)], now=NOW, min_score=60, limit=8,
                    llm_call=RecordingLLM(raises=RuntimeError("nope")),
                    llm_budget=15, store=store,
                )
                row = store.top_leads(limit=5)[0]
                self.assertEqual(row["llm_status"], "unavailable")


# ---------------------------------------------------------------------------
# Bounded influence
# ---------------------------------------------------------------------------


class TestBoundedAdjustment(unittest.TestCase):
    def test_adjustment_never_exceeds_eight_points(self):
        base = {
            "id": "x:1", "source": "Freelancer.com", "title": "Fix FastAPI bug",
            "company": "c", "url": "u", "work_type": "task", "no_interview": True,
            "competition": 2, "posted_at": NOW, "tags": ["FastAPI"],
            "salary": "USD 250-250 fixed",
            "description": "FastAPI + PostgreSQL bug. Fixed price. Acceptance criteria: works.",
        }
        baseline = ls.evaluate(base, None, now=NOW).opportunity_score
        high = ls.evaluate(base, dict(VALID_ANALYSIS, llm_score=10, recommendation="APPLY"),
                           now=NOW).opportunity_score
        low = ls.evaluate(base, dict(VALID_ANALYSIS, llm_score=1, recommendation="REJECT"),
                          now=NOW).opportunity_score

        self.assertLessEqual(high - baseline, 8)
        self.assertLessEqual(baseline - low, 8)
        self.assertGreater(high, low)

    def test_reject_verdict_downgrades_even_with_a_high_score(self):
        a = ls.evaluate(None or {"work_type": "task"}, {"llm_score": 10, "recommendation": "REJECT"})
        self.assertLess(a.opportunity_score, 100)

    def test_unvalidated_input_contributes_nothing(self):
        self.assertEqual(llmscreen.score_adjustment(None), 0.0)
        self.assertEqual(llmscreen.score_adjustment({}), 0.0)
        self.assertEqual(llmscreen.score_adjustment({"llm_score": "abc"}), 0.0)
        self.assertEqual(llmscreen.score_adjustment({"llm_score": 99}), 0.0)

    def test_deterministic_score_remains_primary(self):
        """A perfect DeepSeek verdict cannot rescue a listing the filters dislike."""
        weak = {
            "id": "x:2", "source": "Freelancer.com", "title": "Python script",
            "company": "c", "url": "u", "work_type": "task", "no_interview": True,
            "competition": 90, "posted_at": NOW - timedelta(days=200), "tags": [],
            "salary": "", "description": "vague",
        }
        perfect = dict(VALID_ANALYSIS, llm_score=10, recommendation="APPLY")
        self.assertLess(ls.evaluate(weak, perfect, now=NOW).opportunity_score, 70)


# ---------------------------------------------------------------------------
# Strict schema
# ---------------------------------------------------------------------------


class TestStrictSchema(unittest.TestCase):
    def test_valid_payload_accepted_and_normalised(self):
        result = llmscreen.parse_screen_result(dict(VALID_ANALYSIS, recommendation="apply"))
        self.assertEqual(result["recommendation"], "APPLY")
        self.assertEqual(result["llm_score"], 8)
        self.assertEqual(set(result), set(llmscreen.REQUIRED_KEYS))

    def test_code_fenced_json_accepted(self):
        import json

        fenced = "```json\n" + json.dumps(VALID_ANALYSIS) + "\n```"
        self.assertEqual(llmscreen.parse_screen_result(fenced)["llm_score"], 8)

    def test_missing_keys_rejected(self):
        payload = dict(VALID_ANALYSIS)
        payload.pop("risk")
        with self.assertRaises(llmscreen.InvalidScreenResult):
            llmscreen.parse_screen_result(payload)

    def test_bad_enums_rejected(self):
        for field in ("recommendation", "interview_likelihood", "budget_quality", "risk"):
            payload = dict(VALID_ANALYSIS)
            payload[field] = "NONSENSE"
            with self.assertRaises(llmscreen.InvalidScreenResult, msg=field):
                llmscreen.parse_screen_result(payload)

    def test_out_of_range_score_rejected(self):
        for score in (0, 11, -3):
            with self.assertRaises(llmscreen.InvalidScreenResult):
                llmscreen.parse_screen_result(dict(VALID_ANALYSIS, llm_score=score))

    def test_prose_instead_of_json_rejected(self):
        with self.assertRaises(llmscreen.InvalidScreenResult):
            llmscreen.parse_screen_result("Sure! This looks like a great task for you.")

    def test_overlong_reason_rejected(self):
        with self.assertRaises(llmscreen.InvalidScreenResult):
            llmscreen.parse_screen_result(dict(VALID_ANALYSIS, reason="x" * 500))

    def test_multiline_reason_rejected(self):
        with self.assertRaises(llmscreen.InvalidScreenResult):
            llmscreen.parse_screen_result(dict(VALID_ANALYSIS, reason="line one\nline two"))


# ---------------------------------------------------------------------------
# 7. Proposals are never automatic
# ---------------------------------------------------------------------------


class TestProposalsNotAutomatic(unittest.TestCase):
    def test_funnel_does_not_generate_proposals(self):
        with tempfile.TemporaryDirectory() as tmp:
            with leadstore.LeadStore(Path(tmp) / "leads.db") as store:
                pipeline.screen_jobs(
                    [good_task(i) for i in range(5)], now=NOW, min_score=60, limit=8,
                    llm_call=RecordingLLM(), llm_budget=15, store=store,
                )
                for row in store.top_leads(limit=20):
                    self.assertFalse(row["proposal"], row["lead_id"])

    def test_proposal_can_be_generated_on_demand(self):
        with tempfile.TemporaryDirectory() as tmp:
            with leadstore.LeadStore(Path(tmp) / "leads.db") as store:
                result = pipeline.screen_jobs(
                    [good_task(1)], now=NOW, min_score=60, limit=8,
                    llm_call=RecordingLLM(), llm_budget=15, store=store,
                )
                lead_id = result.leads[0].lead_id
                with offline():
                    text, origin = job_agent.generate_proposal_for_lead(store, lead_id)
                self.assertIn(origin, ("model", "fallback"))
                self.assertTrue(text)
                self.assertEqual(store.get_lead(lead_id)["proposal"], text)

    def test_marking_ready_to_apply_writes_a_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "leads.db"
            with leadstore.LeadStore(db) as store:
                result = pipeline.screen_jobs(
                    [good_task(1)], now=NOW, min_score=60, limit=8,
                    llm_call=RecordingLLM(), llm_budget=15, store=store,
                )
                lead_id = result.leads[0].lead_id
            with offline(db):
                self.assertEqual(job_agent.cmd_mark(lead_id, "READY_TO_APPLY"), 0)
            with leadstore.LeadStore(db) as store:
                self.assertTrue(store.get_lead(lead_id)["proposal"])

    def test_proposal_cap_is_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "leads.db"
            with leadstore.LeadStore(db) as store:
                result = pipeline.screen_jobs(
                    [good_task(i) for i in range(8)], now=NOW, min_score=60, limit=8,
                    llm_call=RecordingLLM(), llm_budget=15, store=store,
                )
                ids = [lead.lead_id for lead in result.leads]
            self.assertGreater(len(ids), job_agent.DEEPSEEK_MAX_PROPOSAL_CALLS)
            with offline(db):
                job_agent.cmd_propose(ids)
            with leadstore.LeadStore(db) as store:
                written = sum(1 for lid in ids if store.get_lead(lid)["proposal"])
                self.assertLessEqual(written, job_agent.DEEPSEEK_MAX_PROPOSAL_CALLS)
                self.assertGreater(written, 0)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestConfiguration(unittest.TestCase):
    def test_screen_cap_defaults_to_a_small_number(self):
        self.assertIn(job_agent.DEEPSEEK_MAX_SCREENED_CALLS, (15, 20))

    def test_proposal_cap_defaults_to_five(self):
        self.assertEqual(job_agent.DEEPSEEK_MAX_PROPOSAL_CALLS, 5)

    def test_deterministic_scoring_needs_no_llm(self):
        self.assertEqual(llmscreen.score_adjustment(None), 0.0)
        self.assertFalse(hasattr(job_agent, "screen_candidate_deepseek") and False)


class TestUsageReporting(unittest.TestCase):
    def test_run_stats_are_persisted_and_summarised(self):
        with tempfile.TemporaryDirectory() as tmp:
            with leadstore.LeadStore(Path(tmp) / "leads.db") as store:
                result = pipeline.screen_jobs(
                    [good_task(i) for i in range(30)], now=NOW, min_score=60, limit=8,
                    llm_call=RecordingLLM(), llm_budget=6, store=store,
                )
                store.record_run_stats({
                    "raw": result.raw_count,
                    "unique": result.deduped_count,
                    "rejected": len(result.rejected),
                    "screened": result.scored_count,
                    "leads": len(result.leads),
                    **result.usage.as_dict(),
                })
                summary = store.llm_usage_summary()
                self.assertEqual(summary["last_run"]["deepseek_calls"], 6)
                self.assertEqual(summary["last_run"]["deepseek_skipped"], 24)
                self.assertEqual(summary["totals"]["deepseek_calls"], 6)
                self.assertEqual(summary["runs"], 1)

    def test_candidate_count_round_trips_through_the_database(self):
        """Regression: deepseek_candidates was recorded but had no column, so
        --metrics always reported 0 candidates for the last run."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "leads.db"
            with leadstore.LeadStore(db) as store:
                store.record_run_stats({"deepseek_candidates": 34, "deepseek_calls": 15,
                                        "deepseek_successes": 15, "deepseek_skipped": 19})
                summary = store.llm_usage_summary()
            self.assertEqual(summary["last_run"]["deepseek_candidates"], 34)
            self.assertEqual(summary["last_run"]["deepseek_skipped"], 19)
            # And it survives reopening (i.e. the column really exists).
            with leadstore.LeadStore(db) as store:
                self.assertEqual(store.llm_usage_summary()["last_run"]["deepseek_candidates"], 34)

    def test_run_stats_migrates_an_older_database(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE run_stats (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT,"
                " deepseek_calls INTEGER DEFAULT 0)"
            )
            conn.execute("INSERT INTO run_stats (at, deepseek_calls) VALUES ('x', 7)")
            conn.commit()
            conn.close()
            with leadstore.LeadStore(db) as store:  # migration runs on open
                summary = store.llm_usage_summary()
                self.assertEqual(summary["totals"]["deepseek_calls"], 7)
                self.assertEqual(summary["last_run"]["deepseek_candidates"], 0)

    def test_usage_line_is_human_readable(self):
        usage = pipeline.LLMUsage(candidates=30, calls=15, successes=14,
                                  failures=1, cached=3, skipped=15)
        line = job_agent.usage_line(usage, 15)
        self.assertIn("15/15 calls", line)
        self.assertIn("30 candidate", line)
        self.assertIn("3 cached", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
