"""Tests for the digest and HTML dashboard rendering."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard  # noqa: E402
import leadstore  # noqa: E402

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def lead(**overrides):
    base = {
        "lead_id": "freelancer.com:1",
        "source": "Freelancer.com",
        "title": "Fix FastAPI upload endpoint",
        "company": "client",
        "url": "https://example.test/1",
        "work_type": "task",
        "opportunity_score": 94,
        "priority": "HIGH PRIORITY",
        "skill_match_pct": 96,
        "interview": "LOW",
        "estimated_hours": 8.0,
        "implied_hourly_usd": 31.0,
        "budget_text": "USD 250 fixed",
        "budget_usd": 250.0,
        "competition": 4,
        "posted_at": (NOW - timedelta(hours=2)).isoformat(),
        "state": "DISCOVERED",
        "why": "Existing FastAPI + PostgreSQL experience directly matches.",
        "red_flags": "[]",
        "matched_skills": '["fastapi", "python"]',
        "proposal": "",
    }
    base.update(overrides)
    return base


class TestFormatting(unittest.TestCase):
    def test_relative_time(self):
        self.assertEqual(dashboard.relative_time((NOW - timedelta(minutes=5)).isoformat(), NOW),
                         "5 min ago")
        self.assertEqual(dashboard.relative_time((NOW - timedelta(hours=2)).isoformat(), NOW),
                         "2 hours ago")
        self.assertEqual(dashboard.relative_time((NOW - timedelta(days=3)).isoformat(), NOW),
                         "3 days ago")
        self.assertEqual(dashboard.relative_time(None, NOW), "unknown")

    def test_relative_time_accepts_datetime(self):
        self.assertEqual(dashboard.relative_time(NOW - timedelta(hours=1), NOW), "1 hour ago")

    def test_format_duration(self):
        self.assertEqual(dashboard.format_duration(3), "a few hours")
        self.assertEqual(dashboard.format_duration(8), "about 1 day")
        self.assertIn("days", dashboard.format_duration(24))
        self.assertEqual(dashboard.format_duration(0), "unknown")

    def test_format_money(self):
        self.assertIn("250", dashboard.format_money(lead()))
        self.assertEqual(dashboard.format_money(lead(budget_text="not stated")), "not stated")

    def test_format_competition(self):
        self.assertEqual(dashboard.format_competition(lead(competition=1)), "1 proposal")
        self.assertEqual(dashboard.format_competition(lead(competition=4)), "4 proposals")
        self.assertEqual(dashboard.format_competition(lead(competition=None)), "unknown")


class TestRecommendedAction(unittest.TestCase):
    def test_scales_with_score_and_state(self):
        self.assertEqual(dashboard.recommended_action(lead(opportunity_score=90)), "APPLY NOW")
        self.assertEqual(dashboard.recommended_action(lead(opportunity_score=70)), "APPLY")
        self.assertEqual(dashboard.recommended_action(lead(opportunity_score=55)), "REVIEW")
        self.assertEqual(dashboard.recommended_action(lead(opportunity_score=20)), "SKIP")

    def test_state_overrides_score(self):
        self.assertEqual(dashboard.recommended_action(lead(state="APPLIED", opportunity_score=95)),
                         "FOLLOW UP")
        self.assertEqual(dashboard.recommended_action(lead(state="COMPLETED", opportunity_score=95)),
                         "INVOICE")
        self.assertEqual(dashboard.recommended_action(lead(state="REJECTED")), "ARCHIVED")


class TestDigest(unittest.TestCase):
    def test_digest_contains_essentials(self):
        text = dashboard.render_digest([lead()], now=NOW)
        self.assertIn("TODAY'S PAID TASK LEADS", text)
        self.assertIn("Fix FastAPI upload endpoint", text)
        self.assertIn("94/100", text)
        self.assertIn("Freelancer.com", text)
        self.assertIn("https://example.test/1", text)
        self.assertIn("APPLY NOW", text)

    def test_digest_reports_summary(self):
        text = dashboard.render_digest([lead(), lead(lead_id="b:2", budget_usd=150)], now=NOW)
        self.assertIn("Top opportunities shown: 2", text)
        self.assertIn("400", text)  # 250 + 150 gross

    def test_empty_digest_is_explicit_about_quality(self):
        text = dashboard.render_digest([], now=NOW)
        self.assertIn("No leads cleared the quality bar", text)
        self.assertIn("not lowered", text)

    def test_digest_includes_metrics_and_revenue(self):
        store_metrics = type("M", (), {"as_dict": lambda self: {
            "days": 30, "discovered": 500, "qualified": 40, "applied": 3,
            "replied": 1, "won": 0, "paid": 0, "reply_rate_pct": 33.3,
        }})()
        revenue = type("R", (), {"as_dict": lambda self: {
            "collected_usd": 0, "collected_inr": 0, "pending_usd": 0,
            "pending_inr": 0, "debt_target_inr": 418000,
            "debt_deadline": "2026-12-31", "debt_reduction_pct": 0,
            "remaining_inr": 418000, "won_usd": 0,
        }})()
        text = dashboard.render_digest([lead()], store_metrics, revenue, now=NOW)
        self.assertIn("KPI that matters", text)
        self.assertIn("418,000", text)

    def test_digest_handles_red_flags(self):
        text = dashboard.render_digest([lead(red_flags='["small absolute budget"]')], now=NOW)
        self.assertIn("RED FLAGS", text)
        self.assertIn("small absolute budget", text)


class TestHtmlDashboard(unittest.TestCase):
    def test_renders_self_contained_html(self):
        html = dashboard.render_dashboard_html([lead()], now=NOW)
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("Today's Best Paid Tasks", html)
        self.assertIn("<style>", html)
        # No external assets: must work offline.
        self.assertNotIn("http://cdn", html)
        self.assertNotIn("https://cdn", html)

    def test_shows_required_fields(self):
        html = dashboard.render_dashboard_html([lead()], now=NOW)
        for expected in ("94", "HIGH PRIORITY", "Fix FastAPI upload endpoint",
                         "Freelancer.com", "APPLY NOW", "USD 250 fixed",
                         "https://example.test/1", "LOW", "4 proposals"):
            self.assertIn(expected, html, expected)

    def test_empty_state(self):
        html = dashboard.render_dashboard_html([], now=NOW)
        self.assertIn("No leads cleared the quality bar", html)

    def test_html_is_escaped(self):
        nasty = lead(title='<script>alert("xss")</script>', why="<img src=x onerror=1>")
        html = dashboard.render_dashboard_html([nasty], now=NOW)
        # The dangerous thing is an unescaped tag; the same characters are fine
        # as escaped text content.
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<img src=x onerror=1>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;img src=x onerror=1&gt;", html)

    def test_url_is_escaped_in_href(self):
        nasty = lead(url='https://x.test/"><script>alert(1)</script>')
        html = dashboard.render_dashboard_html([nasty], now=NOW)
        self.assertNotIn('"><script>', html)

    def test_proposal_rendered_when_present(self):
        html = dashboard.render_dashboard_html([lead(proposal="My concrete plan.")], now=NOW)
        self.assertIn("Proposal draft", html)
        self.assertIn("My concrete plan.", html)

    def test_pipeline_section(self):
        rows = [lead(state="APPLIED", title="Applied task")]
        html = dashboard.render_dashboard_html([], pipeline=rows, now=NOW)
        self.assertIn("Pipeline", html)
        self.assertIn("Applied task", html)

    def test_write_dashboard_creates_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sub" / "dashboard.html"
            written = dashboard.write_dashboard(target, "<html></html>")
            self.assertTrue(written.exists())
            self.assertEqual(written.read_text(encoding="utf-8"), "<html></html>")


class TestRealSqliteRows(unittest.TestCase):
    """The renderers must work with sqlite3.Row, not just dicts."""

    def test_renders_from_store(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with leadstore.LeadStore(Path(tmp) / "leads.db") as store:
                store.upsert_lead({
                    "lead_id": "x:1", "source": "Mercor", "title": "Agent Engineer",
                    "company": "c", "url": "https://x/1", "work_type": "task",
                    "opportunity_score": 88, "priority": "HIGH PRIORITY",
                    "skill_match_pct": 90, "interview": "NONE", "estimated_hours": 6,
                    "implied_hourly_usd": 40, "budget_text": "USD 240 fixed",
                    "budget_usd": 240, "competition": 2, "why": "matches",
                    "red_flags": [], "matched_skills": ["python"],
                })
                rows = store.top_leads(limit=10)
                self.assertEqual(len(rows), 1)
                text = dashboard.render_digest(rows, now=NOW)
                html = dashboard.render_dashboard_html(rows, now=NOW)
                self.assertIn("Agent Engineer", text)
                self.assertIn("Agent Engineer", html)
                self.assertIn("88", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
