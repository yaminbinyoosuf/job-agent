"""Tests for proposal drafting: grounding, anti-fabrication, fallbacks."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import leadscore as ls  # noqa: E402
import proposals  # noqa: E402

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def job(**overrides):
    base = {
        "id": "freelancer.com:1",
        "source": "Freelancer.com",
        "title": "Fix FastAPI upload endpoint returning 500",
        "company": "client",
        "url": "https://example.test/1",
        "work_type": "task",
        "no_interview": True,
        "salary": "USD 250-250 fixed",
        "competition": 4,
        "posted_at": NOW,
        "tags": ["Python", "FastAPI"],
        "description": (
            "FastAPI + PostgreSQL app. The /upload endpoint returns 500 on files "
            "over 10MB. Fix it and add a regression test. Fixed price."
        ),
    }
    base.update(overrides)
    return base


class TestFactSheet(unittest.TestCase):
    def test_fact_sheet_is_non_empty_and_specific(self):
        self.assertGreater(len(proposals.FACT_SHEET), 5)
        blob = " ".join(proposals.FACT_SHEET).lower()
        self.assertIn("fastapi", blob)
        self.assertIn("thryvix", blob)
        self.assertIn("cogext", blob)

    def test_forbidden_claims_cover_the_obvious_lies(self):
        blob = " ".join(proposals.DISALLOWED_CLAIMS).lower()
        self.assertIn("led a team", blob)
        self.assertIn("faang", blob)


class TestPrompt(unittest.TestCase):
    def test_prompt_references_the_actual_task(self):
        ev = ls.evaluate(job(), now=NOW)
        prompt = proposals.build_proposal_prompt(job(), ev)
        self.assertIn("Fix FastAPI upload endpoint returning 500", prompt)
        self.assertIn("FastAPI + PostgreSQL app", prompt)
        self.assertIn("USD 250", prompt)

    def test_prompt_embeds_the_fact_sheet_and_forbids_invention(self):
        ev = ls.evaluate(job(), now=NOW)
        prompt = proposals.build_proposal_prompt(job(), ev)
        self.assertIn("THRYVIX", prompt)
        self.assertIn("FORBIDDEN CLAIMS", prompt)
        self.assertIn("Do not go beyond it", prompt)

    def test_prompt_asks_for_json_only(self):
        ev = ls.evaluate(job(), now=NOW)
        self.assertIn("ONLY a JSON object", proposals.build_proposal_prompt(job(), ev))


class TestFallback(unittest.TestCase):
    def test_fallback_mentions_the_task_title(self):
        ev = ls.evaluate(job(), now=NOW)
        text = proposals.fallback_proposal(job(), ev)
        self.assertIn("Fix FastAPI upload endpoint returning 500", text)

    def test_fallback_cites_real_skills_when_matched(self):
        ev = ls.evaluate(job(), now=NOW)
        text = proposals.fallback_proposal(job(), ev)
        self.assertTrue(any(skill in text.lower() for skill in ("fastapi", "python", "postgresql")))

    def test_fallback_contains_no_forbidden_claims(self):
        ev = ls.evaluate(job(), now=NOW)
        text = proposals.fallback_proposal(job(), ev).lower()
        for claim in proposals.DISALLOWED_CLAIMS:
            self.assertNotIn(claim, text)

    def test_delivery_scales_with_effort(self):
        quick = proposals.fallback_proposal(job(), ls.evaluate(job(description="1 hour script"), now=NOW))
        slow = proposals.fallback_proposal(
            job(description="Big migration, about 3-5 days of work"), now=None
        ) if False else proposals.fallback_proposal(
            job(), ls.evaluate(job(description="About 2 weeks of work"), now=NOW)
        )
        self.assertIn("within a day", quick)
        self.assertIn("milestone", slow)


class TestGenerate(unittest.TestCase):
    def test_uses_model_output_when_clean(self):
        ev = ls.evaluate(job(), now=NOW)
        text, origin = proposals.generate_proposal(
            job(), ev, None, llm_call=lambda _p: {"proposal": "A specific, grounded plan."}
        )
        self.assertEqual(origin, "model")
        self.assertEqual(text, "A specific, grounded plan.")

    def test_rejects_a_forbidden_claim_and_falls_back(self):
        ev = ls.evaluate(job(), now=NOW)
        text, origin = proposals.generate_proposal(
            job(), ev, None,
            llm_call=lambda _p: {"proposal": "I led a team of 20 engineers at scale."},
        )
        self.assertEqual(origin, "fallback")
        self.assertNotIn("led a team", text.lower())

    def test_falls_back_when_model_errors(self):
        def boom(_p):
            raise RuntimeError("model down")

        ev = ls.evaluate(job(), now=NOW)
        text, origin = proposals.generate_proposal(job(), ev, None, llm_call=boom)
        self.assertEqual(origin, "fallback")
        self.assertTrue(text)

    def test_falls_back_on_empty_output(self):
        ev = ls.evaluate(job(), now=NOW)
        _text, origin = proposals.generate_proposal(job(), ev, None, llm_call=lambda _p: {})
        self.assertEqual(origin, "fallback")

    def test_no_llm_call_still_produces_a_proposal(self):
        ev = ls.evaluate(job(), now=NOW)
        text, origin = proposals.generate_proposal(job(), ev, None, llm_call=None)
        self.assertEqual(origin, "fallback")
        self.assertGreater(len(text), 80)


if __name__ == "__main__":
    unittest.main(verbosity=2)
