"""Tests for the lead store: lifecycle, dedupe, metrics and revenue."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import leadstore  # noqa: E402


def record(lead_id="freelancer.com:1", score=88, **overrides):
    base = {
        "lead_id": lead_id,
        "source": "Freelancer.com",
        "title": "Fix FastAPI upload endpoint",
        "company": "client",
        "url": "https://example.test/1",
        "work_type": "task",
        "opportunity_score": score,
        "priority": "HIGH PRIORITY",
        "skill_match_pct": 96,
        "interview": "NONE",
        "estimated_hours": 8.0,
        "implied_hourly_usd": 31.0,
        "budget_text": "USD 250 fixed",
        "budget_usd": 250.0,
        "competition": 4,
        "location": "Remote",
        "posted_at": datetime.now(timezone.utc).isoformat(),
        "reason": "FastAPI + PostgreSQL match",
        "why": "Direct stack match on a bounded bug fix.",
        "red_flags": [],
        "matched_skills": ["fastapi", "python"],
    }
    base.update(overrides)
    return base


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "leads.db"
        self.store = leadstore.LeadStore(self.path)

    def tearDown(self):
        self.store.close()
        self._dir.cleanup()


class TestSchema(StoreCase):
    def test_tables_created(self):
        names = {
            row[0]
            for row in self.store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue({"leads", "lead_events", "revenue"}.issubset(names))

    def test_database_file_created(self):
        self.assertTrue(self.path.exists())


class TestUpsert(StoreCase):
    def test_new_lead(self):
        self.assertEqual(self.store.upsert_lead(record()), "new")
        self.assertIsNotNone(self.store.get_lead("freelancer.com:1"))

    def test_duplicate_is_not_inserted_twice(self):
        self.store.upsert_lead(record())
        self.store.upsert_lead(record())
        count = self.store.conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
        self.assertEqual(count, 1)

    def test_rescore_is_reported(self):
        self.store.upsert_lead(record(score=60))
        self.assertEqual(self.store.upsert_lead(record(score=90)), "rescored")

    def test_unchanged_is_reported(self):
        self.store.upsert_lead(record(score=90))
        self.assertEqual(self.store.upsert_lead(record(score=90)), "unchanged")

    def test_rescore_preserves_state_and_proposal(self):
        """Human decisions must survive a rescore."""
        self.store.upsert_lead(record(score=60))
        self.store.set_state("freelancer.com:1", "APPLIED", "sent proposal")
        self.store.set_proposal("freelancer.com:1", "my proposal")
        self.store.upsert_lead(record(score=95))
        row = self.store.get_lead("freelancer.com:1")
        self.assertEqual(row["state"], "APPLIED")
        self.assertEqual(row["proposal"], "my proposal")
        self.assertEqual(row["opportunity_score"], 95)


class TestLifecycle(StoreCase):
    def test_full_happy_path(self):
        self.store.upsert_lead(record())
        for state in ("QUALIFIED", "SHORTLISTED", "READY_TO_APPLY", "APPLIED",
                      "REPLIED", "WON", "IN_PROGRESS", "COMPLETED", "PAID"):
            self.assertTrue(self.store.set_state("freelancer.com:1", state), state)
        self.assertEqual(self.store.get_lead("freelancer.com:1")["state"], "PAID")

    def test_every_state_change_is_recorded(self):
        self.store.upsert_lead(record())
        self.store.set_state("freelancer.com:1", "SHORTLISTED")
        self.store.set_state("freelancer.com:1", "APPLIED", "sent")
        events = self.store.events("freelancer.com:1")
        self.assertEqual(events[0]["to_state"], "DISCOVERED")
        self.assertEqual(events[-1]["to_state"], "APPLIED")
        self.assertEqual(events[-1]["note"], "sent")

    def test_off_path_states_supported(self):
        self.store.upsert_lead(record())
        for state in ("REJECTED", "IGNORED", "EXPIRED"):
            self.assertTrue(self.store.set_state("freelancer.com:1", state))

    def test_unknown_state_raises(self):
        self.store.upsert_lead(record())
        with self.assertRaises(ValueError):
            self.store.set_state("freelancer.com:1", "NOT_A_STATE")

    def test_set_state_on_missing_lead_returns_false(self):
        self.assertFalse(self.store.set_state("nope", "APPLIED"))

    def test_applied_increments_actions_taken(self):
        self.store.upsert_lead(record())
        self.store.set_state("freelancer.com:1", "APPLIED")
        self.assertEqual(self.store.get_lead("freelancer.com:1")["actions_taken"], 1)


class TestQueries(StoreCase):
    def test_top_leads_sorted_and_limited(self):
        for i in range(8):
            self.store.upsert_lead(record(lead_id=f"x:{i}", score=50 + i * 5))
        top = self.store.top_leads(limit=5, min_score=0)
        self.assertEqual(len(top), 5)
        scores = [row["opportunity_score"] for row in top]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_top_leads_respects_min_score(self):
        self.store.upsert_lead(record(lead_id="x:low", score=30))
        self.store.upsert_lead(record(lead_id="x:high", score=90))
        top = self.store.top_leads(limit=10, min_score=60)
        self.assertEqual([row["lead_id"] for row in top], ["x:high"])

    def test_closed_leads_are_excluded(self):
        self.store.upsert_lead(record(lead_id="x:1"))
        self.store.set_state("x:1", "REJECTED")
        self.assertEqual(self.store.top_leads(limit=10), [])

    def test_applied_leads_leave_the_dashboard(self):
        self.store.upsert_lead(record(lead_id="x:1"))
        self.store.set_state("x:1", "APPLIED")
        self.assertEqual(self.store.top_leads(limit=10), [])
        self.assertEqual(len(self.store.pipeline()), 1)

    def test_same_lead_never_appears_twice(self):
        self.store.upsert_lead(record(lead_id="x:1", score=90))
        self.store.upsert_lead(record(lead_id="x:1", score=91))
        top = self.store.top_leads(limit=10)
        self.assertEqual(len(top), 1)

    def test_count_by_state(self):
        self.store.upsert_lead(record(lead_id="x:1"))
        self.store.upsert_lead(record(lead_id="x:2"))
        self.store.set_state("x:2", "APPLIED")
        counts = self.store.count_by_state()
        self.assertEqual(counts.get("DISCOVERED"), 1)
        self.assertEqual(counts.get("APPLIED"), 1)

    def test_stale_leads_expire(self):
        self.store.upsert_lead(record(lead_id="x:old"))
        old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        self.store.conn.execute(
            "UPDATE leads SET discovered_at = ? WHERE lead_id = ?", (old, "x:old")
        )
        self.store.conn.commit()
        self.store.mark_stale_as_expired(max_age_days=45)
        self.assertEqual(self.store.get_lead("x:old")["state"], "EXPIRED")

    def test_fresh_leads_are_not_expired(self):
        self.store.upsert_lead(record(lead_id="x:new"))
        self.store.mark_stale_as_expired(max_age_days=45)
        self.assertEqual(self.store.get_lead("x:new")["state"], "DISCOVERED")


class TestRevenue(StoreCase):
    def test_pending_and_collected(self):
        self.store.upsert_lead(record())
        self.store.record_revenue("freelancer.com:1", 250, "USD", "pending")
        self.store.record_revenue("freelancer.com:1", 100, "USD", "collected")
        summary = self.store.revenue_summary()
        self.assertEqual(summary.pending_usd, 250)
        self.assertEqual(summary.collected_usd, 100)
        self.assertEqual(summary.won_usd, 350)

    def test_inr_conversion(self):
        self.store.upsert_lead(record())
        self.store.record_revenue("freelancer.com:1", 100, "USD", "collected")
        summary = self.store.revenue_summary()
        self.assertAlmostEqual(summary.collected_inr, 100 * leadstore.USD_TO_INR, places=1)

    def test_debt_progress(self):
        self.store.upsert_lead(record())
        self.store.record_revenue(
            "freelancer.com:1", leadstore.USD_TO_INR, "INR", "collected"
        )  # 1 USD worth of INR
        summary = self.store.revenue_summary(
            debt_target_inr=leadstore.USD_TO_INR, debt_deadline="2026-12-31"
        )
        self.assertAlmostEqual(summary.debt_reduction_pct, 100.0, places=1)
        self.assertAlmostEqual(summary.remaining_inr, 0.0, places=1)

    def test_invalid_status_raises(self):
        self.store.upsert_lead(record())
        with self.assertRaises(ValueError):
            self.store.record_revenue("freelancer.com:1", 10, "USD", "maybe")


class TestMetrics(StoreCase):
    def test_funnel_counts(self):
        for i in range(5):
            self.store.upsert_lead(record(lead_id=f"x:{i}", score=70 + i))
        self.store.set_state("x:0", "APPLIED")
        self.store.set_state("x:1", "APPLIED")
        self.store.set_state("x:2", "APPLIED")
        self.store.set_state("x:0", "REPLIED")
        self.store.set_state("x:0", "WON")
        metrics = self.store.funnel_metrics(days=30)
        self.assertEqual(metrics.discovered, 5)
        self.assertEqual(metrics.applied, 3)
        self.assertEqual(metrics.replied, 1)
        self.assertEqual(metrics.won, 1)
        self.assertAlmostEqual(metrics.win_rate, 100 / 3, places=1)

    def test_rates_are_zero_when_no_applications(self):
        metrics = self.store.funnel_metrics(days=30)
        self.assertEqual(metrics.reply_rate, 0.0)
        self.assertEqual(metrics.win_rate, 0.0)

    def test_metrics_dict_serialisable(self):
        import json

        json.dumps(self.store.funnel_metrics(days=7).as_dict())


class TestLegacyImport(StoreCase):
    def test_import_marks_rows_ignored(self):
        csv_path = Path(self._dir.name) / "jobs_log.csv"
        csv_path.write_text(
            "timestamp,job_id,source,title,company,url,score,emailed,reason,rubric\n"
            '2026-08-22T19:59:00+00:00,arbeitnow:old,Arbeitnow,Old Job,X,https://x/1,8,True,"was a match",""\n',
            encoding="utf-8",
        )
        imported = self.store.import_legacy_csv(csv_path)
        self.assertEqual(imported, 1)
        row = self.store.get_lead("arbeitnow:old")
        self.assertEqual(row["state"], "IGNORED")
        # Legacy rows must never resurface as today's leads.
        self.assertEqual(self.store.top_leads(limit=10), [])

    def test_import_is_idempotent(self):
        csv_path = Path(self._dir.name) / "jobs_log.csv"
        csv_path.write_text(
            "timestamp,job_id,source,title,company,url,score,emailed,reason,rubric\n"
            '2026-08-22T19:59:00+00:00,arbeitnow:old,Arbeitnow,Old Job,X,https://x/1,8,True,"r",""\n',
            encoding="utf-8",
        )
        self.store.import_legacy_csv(csv_path)
        self.assertEqual(self.store.import_legacy_csv(csv_path), 0)

    def test_missing_csv_is_harmless(self):
        self.assertEqual(self.store.import_legacy_csv("/nonexistent/x.csv"), 0)

    def test_non_emailed_rows_are_not_imported(self):
        """Regression: importing every historical row blocked hundreds of
        listings that the new task-oriented scoring would rate highly."""
        csv_path = Path(self._dir.name) / "jobs_log.csv"
        csv_path.write_text(
            "timestamp,job_id,source,title,company,url,score,emailed,reason,rubric\n"
            '2026-08-22T19:59:00+00:00,a:emailed,Arbeitnow,Sent,X,https://x/1,8,True,"match",""\n'
            '2026-08-22T19:59:01+00:00,a:notsent,Arbeitnow,Screened,X,https://x/2,3,False,"below bar",""\n',
            encoding="utf-8",
        )
        self.assertEqual(self.store.import_legacy_csv(csv_path), 1)
        self.assertIsNotNone(self.store.get_lead("a:emailed"))
        self.assertIsNone(self.store.get_lead("a:notsent"))

    def test_rejected_rows_are_not_imported(self):
        """The audit CSV must never turn into a blacklist."""
        csv_path = Path(self._dir.name) / "jobs_log.csv"
        csv_path.write_text(
            "timestamp,job_id,source,title,company,url,score,emailed,reason,rubric\n"
            '2026-08-22T19:59:00+00:00,a:rej,Arbeitnow,Dropped,X,https://x/3,,False,'
            '"rejected: full_time, long_term",""\n',
            encoding="utf-8",
        )
        self.assertEqual(self.store.import_legacy_csv(csv_path), 0)
        self.assertIsNone(self.store.get_lead("a:rej"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
