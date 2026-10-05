"""Tests for the hard rejection filters and the opportunity scoring engine."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import leadscore as ls  # noqa: E402

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def job(**overrides):
    """A realistic, winnable fixed-price task, with fields overridable."""
    base = {
        "id": "freelancer.com:1",
        "source": "Freelancer.com",
        "title": "Fix FastAPI endpoint returning 500 on file upload",
        "company": "client",
        "url": "https://example.test/1",
        "work_type": "task",
        "no_interview": True,
        "competition": 4,
        "posted_at": NOW - timedelta(hours=2),
        "tags": ["Python", "FastAPI"],
        "salary": "USD 250-250 fixed",
        "description": (
            "We have a FastAPI + PostgreSQL app. The /upload endpoint returns 500 on "
            "large files and needs fixing. Small task, fixed price. Acceptance "
            "criteria: uploads over 10MB succeed."
        ),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Budget parsing
# ---------------------------------------------------------------------------


class TestBudget(unittest.TestCase):
    def test_symbol_range_with_fixed(self):
        b = ls.parse_budget("$250-500 fixed")
        self.assertIsNotNone(b)
        self.assertEqual((b.low, b.high), (250, 500))
        self.assertEqual(b.currency, "USD")
        self.assertTrue(b.fixed)

    def test_currency_code(self):
        b = ls.parse_budget("USD 250-750 · 51 bids")
        self.assertEqual((b.low, b.high), (250, 750))
        self.assertEqual(b.usd_estimate, 500)

    def test_inr_converted_to_usd(self):
        b = ls.parse_budget("INR 12500-37500")
        self.assertGreater(b.usd_high, b.usd_low)
        self.assertLess(b.usd_estimate, 500)  # sanity: not treated as dollars

    def test_hourly_detected(self):
        b = ls.parse_budget("$70-$150/hourly")
        self.assertTrue(b.hourly)

    def test_monthly_detected(self):
        b = ls.parse_budget("$1000/month")
        self.assertTrue(b.monthly)

    def test_no_budget_returns_none(self):
        self.assertIsNone(ls.parse_budget(""))
        self.assertIsNone(ls.parse_budget("competitive salary"))

    def test_malformed_amounts_do_not_crash(self):
        """Regression: a character class of [\\d,]+ matches a lone comma, which
        produced float('') and killed a live run."""
        for text in ("USD ,", "USD", "$", "₹ ,", "EUR ,,", ",,", "USD -", "$-"):
            self.assertIsNone(ls.parse_budget(text), text)

    def test_parse_budget_never_raises_on_odd_input(self):
        for text in ("", None, "  ", "$$$", "USD 1,2,3,4", "\u20b9", "£0"):
            try:
                ls.parse_budget(text)
            except Exception as exc:  # noqa: BLE001
                self.fail(f"parse_budget({text!r}) raised {exc!r}")

    def test_describe_mentions_currency(self):
        self.assertIn("USD", ls.parse_budget("USD 300").describe())


# ---------------------------------------------------------------------------
# Duration
# ---------------------------------------------------------------------------


class TestEstimateHours(unittest.TestCase):
    def test_explicit_range_days(self):
        hours = ls.estimate_hours({"description": "This should take 1-3 days."})
        self.assertEqual(hours, 16.0)  # midpoint 2 days * 8

    def test_explicit_single_hours(self):
        hours = ls.estimate_hours({"description": "A quick task, about 4 hours."})
        self.assertEqual(hours, 4.0)

    def test_phrase_fallback(self):
        self.assertEqual(ls.estimate_hours({"description": "Should be a few hours"}), 3.0)

    def test_budget_heuristic_when_silent(self):
        small = ls.estimate_hours({"description": "Do the thing"}, ls.Budget(low=40, high=40))
        large = ls.estimate_hours({"description": "Do the thing"}, ls.Budget(low=5000, high=5000))
        self.assertLess(small, large)

    def test_never_zero(self):
        self.assertGreater(ls.estimate_hours({"description": ""}), 0)


# ---------------------------------------------------------------------------
# Interview classification
# ---------------------------------------------------------------------------


class TestInterview(unittest.TestCase):
    def test_source_metadata_wins(self):
        self.assertEqual(
            ls.classify_interview({"work_type": "task", "no_interview": True}), "NONE"
        )

    def test_explicit_no_interview_text(self):
        j = {"work_type": "contract", "description": "No interview, start immediately."}
        self.assertEqual(ls.classify_interview(j), "NONE")

    def test_heavy_process_is_high(self):
        j = {
            "work_type": "full_time",
            "description": "Our interview process has multiple rounds and a whiteboard.",
        }
        self.assertEqual(ls.classify_interview(j), "HIGH")

    def test_absence_of_promise_is_not_a_rejection(self):
        """The spec is explicit: do not reject just because 'no interview' is absent."""
        j = job(description="Build a small Python script to rename files. Fixed price $200.")
        j.pop("no_interview")
        ev = ls.evaluate(j, now=NOW)
        self.assertNotIn("no_interview", ev.rejections)
        self.assertIn(ev.interview, ("NONE", "LOW", "MEDIUM", "HIGH"))


# ---------------------------------------------------------------------------
# Component scores
# ---------------------------------------------------------------------------


class TestComponents(unittest.TestCase):
    def test_skill_match_strong_for_core_stack(self):
        score, matched = ls.skill_match(job())
        self.assertGreaterEqual(score, 60)
        self.assertIn("fastapi", matched)
        self.assertIn("python", matched)

    def test_skill_match_zero_for_unrelated(self):
        score, _ = ls.skill_match(job(title="Warehouse operative", description="Lift boxes.", tags=[]))
        self.assertEqual(score, 0)

    def test_task_clarity_rewards_concrete(self):
        concrete, _ = ls.task_clarity(job())
        vague, _ = ls.task_clarity(
            job(description="Ongoing long-term work, various tasks, as needed. Rockstar wanted.")
        )
        self.assertGreater(concrete, vague)

    def test_payment_quality_bands(self):
        low = ls.payment_quality(ls.Budget(low=30, high=30, fixed=True), 8)
        high = ls.payment_quality(ls.Budget(low=800, high=800, fixed=True), 8)
        self.assertLess(low, high)

    def test_payment_quality_hourly_uses_rate(self):
        good = ls.payment_quality(ls.Budget(low=60, high=60, hourly=True), 8)
        poor = ls.payment_quality(ls.Budget(low=5, high=5, hourly=True), 8)
        self.assertGreater(good, poor)

    def test_short_duration_monotonic(self):
        self.assertGreater(ls.short_duration(2), ls.short_duration(20))
        self.assertGreater(ls.short_duration(20), ls.short_duration(200))

    def test_freshness_monotonic(self):
        fresh = ls.freshness(job(posted_at=NOW - timedelta(hours=1)), now=NOW)
        stale = ls.freshness(job(posted_at=NOW - timedelta(days=60)), now=NOW)
        self.assertGreater(fresh, stale)

    def test_low_competition_monotonic(self):
        self.assertGreater(ls.low_competition({"competition": 1}),
                           ls.low_competition({"competition": 50}))
        self.assertEqual(ls.low_competition({"competition": None}), 50)

    def test_fixed_price_prefers_tasks(self):
        task = ls.fixed_price_score({"work_type": "task"}, None)
        full = ls.fixed_price_score({"work_type": "full_time"}, None)
        self.assertGreater(task, full)


# ---------------------------------------------------------------------------
# Hard rejection filters
# ---------------------------------------------------------------------------


class TestRejectionFilters(unittest.TestCase):
    def reasons(self, **overrides):
        return ls.rejection_reasons(job(**overrides), now=NOW)

    def test_good_task_is_not_rejected(self):
        self.assertEqual(self.reasons(), [])

    def test_full_time_rejected(self):
        self.assertIn("full_time", self.reasons(work_type="full_time"))

    def test_internship_rejected(self):
        self.assertIn("internship", self.reasons(description="This is an internship position."))

    def test_unpaid_rejected(self):
        self.assertIn("unpaid", self.reasons(description="This is an unpaid test task."))

    def test_volunteer_rejected(self):
        self.assertIn("volunteer", self.reasons(description="Volunteer developers wanted."))

    def test_contract_to_hire_rejected(self):
        self.assertIn(
            "contract_to_hire",
            self.reasons(description="This is a contract-to-hire role."),
        )

    def test_long_term_rejected(self):
        self.assertIn(
            "long_term",
            self.reasons(description="Long-term engagement expected over 6 months."),
        )

    def test_commission_only_rejected(self):
        self.assertIn(
            "commission_only",
            self.reasons(description="Commission-only compensation."),
        )

    def test_no_deliverable_rejected(self):
        self.assertIn(
            "no_deliverable",
            self.reasons(title="Engineer", description="Interesting work.", salary="",
                         tags=[], work_type="contract"),
        )

    def test_budget_too_low_rejected(self):
        # $40 for 40 hours of work
        self.assertIn(
            "budget_too_low",
            self.reasons(salary="$40", description="Build a complete platform with auth, "
                                                   "payments and an admin dashboard over several weeks."),
        )

    def test_no_budget_only_rejected_when_required(self):
        j = job(salary="", description="Small Python script needed, fixed scope.")
        self.assertNotIn("no_budget", ls.rejection_reasons(j, now=NOW))
        self.assertIn("no_budget", ls.rejection_reasons(j, now=NOW, require_budget=True))

    def test_location_excluded_rejected(self):
        self.assertIn("location_excluded", self.reasons(location_excluded=True))

    def test_scam_rejected(self):
        self.assertIn(
            "scam",
            self.reasons(description="Small Python task. Please pay a registration fee first."),
        )

    def test_unqualified_rejected(self):
        self.assertIn(
            "unqualified",
            self.reasons(description="Requires PhD required in physics and 10+ years."),
        )

    def test_expired_rejected(self):
        self.assertIn(
            "expired",
            self.reasons(posted_at=NOW - timedelta(days=400)),
        )

    def test_explicit_expired_flag(self):
        self.assertIn("expired", self.reasons(expired=True))

    def test_too_many_hours_rejected(self):
        self.assertIn(
            "too_many_hours",
            self.reasons(work_type="contract", hours_per_week=30, salary=""),
        )

    def test_reasons_are_deduplicated(self):
        reasons = self.reasons(work_type="full_time", description="full-time permanent role")
        self.assertEqual(len(reasons), len(set(reasons)))


# ---------------------------------------------------------------------------
# Composite scoring
# ---------------------------------------------------------------------------


class TestOpportunityScore(unittest.TestCase):
    def test_weights_sum_to_100(self):
        self.assertEqual(sum(ls.WEIGHTS.values()), 100)

    def test_good_task_scores_high(self):
        ev = ls.evaluate(job(), now=NOW)
        self.assertGreaterEqual(ev.opportunity_score, 75)
        self.assertEqual(ev.priority, "HIGH PRIORITY")
        self.assertEqual(ev.interview, "NONE")
        self.assertFalse(ev.rejected)

    def test_full_time_role_is_rejected_and_scores_low(self):
        ev = ls.evaluate(
            job(
                source="RemoteOK",
                work_type="full_time",
                no_interview=None,
                salary="$1000/month",
                competition=None,
                description="Full-time permanent AI Engineer. Multiple rounds of interviews. "
                            "20+ hours/week. Long-term.",
            ),
            now=NOW,
        )
        self.assertTrue(ev.rejected)
        self.assertLess(ev.opportunity_score, 55)

    def test_spec_example_ordering(self):
        """The spec's worked example: a $250 fixed bug fix must outrank a
        $1000/month long-term interview role."""
        strong = ls.evaluate(job(), now=NOW)
        weak = ls.evaluate(
            job(
                title="AI Engineer",
                salary="$1000/month",
                work_type="full_time",
                no_interview=None,
                competition=None,
                description="Long-term role, 20+ hours/week, interview required.",
            ),
            now=NOW,
        )
        self.assertGreater(strong.opportunity_score, weak.opportunity_score)
        self.assertGreater(strong.opportunity_score - weak.opportunity_score, 20)

    def test_unknown_budget_is_weak_not_fatal(self):
        ev = ls.evaluate(job(salary=""), now=NOW)
        self.assertFalse(ev.rejected)
        self.assertEqual(ev.expected_value_usd, 0.0)

    def test_llm_score_adjusts_but_does_not_dominate(self):
        base = ls.evaluate(job(), now=NOW).opportunity_score
        boosted = ls.evaluate(job(), {"score": 10}, now=NOW).opportunity_score
        crushed = ls.evaluate(job(), {"score": 1}, now=NOW).opportunity_score
        self.assertGreater(boosted, base)
        self.assertLess(crushed, base)
        self.assertLessEqual(boosted - crushed, 20)  # bounded influence

    def test_score_is_bounded(self):
        for llm in ({"score": 10}, {"score": 1}, None, {"score": "nonsense"}):
            score = ls.evaluate(job(), llm, now=NOW).opportunity_score
            self.assertGreaterEqual(score, 0)
            self.assertLessEqual(score, 100)

    def test_to_dict_is_json_friendly(self):
        import json

        payload = ls.evaluate(job(), now=NOW).to_dict()
        json.dumps(payload)  # must not raise
        self.assertIn("opportunity_score", payload)
        self.assertIn("budget_usd", payload)

    def test_red_flags_and_positives_populated(self):
        ev = ls.evaluate(job(), now=NOW)
        self.assertTrue(ev.positive_signals)


if __name__ == "__main__":
    unittest.main(verbosity=2)
