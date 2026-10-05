"""SQLite persistence for scored task leads: lifecycle, funnel metrics, revenue.

The project previously tracked everything in ``jobs_log.csv``. That file is
still written for backwards compatibility, but a CSV cannot express a lead
lifecycle or revenue, so this module adds a small SQLite store.

Design notes
------------
* No new dependencies — ``sqlite3`` is in the standard library.
* Writes are idempotent: re-discovering a lead updates it in place rather than
  inserting a duplicate, so the same opportunity is never shown twice.
* Every state change is appended to ``lead_events``, giving an audit trail and
  the raw data for the funnel KPIs.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

# Ordered happy path.
LEAD_STATES: tuple[str, ...] = (
    "DISCOVERED",
    "QUALIFIED",
    "SHORTLISTED",
    "READY_TO_APPLY",
    "APPLIED",
    "REPLIED",
    "WON",
    "IN_PROGRESS",
    "COMPLETED",
    "PAID",
)

# Off-path states.
OFF_PATH_STATES: tuple[str, ...] = ("REJECTED", "IGNORED", "EXPIRED")

ALL_STATES: tuple[str, ...] = LEAD_STATES + OFF_PATH_STATES

# States still needing action from the candidate, i.e. dashboard material.
OPEN_STATES: tuple[str, ...] = (
    "DISCOVERED",
    "QUALIFIED",
    "SHORTLISTED",
    "READY_TO_APPLY",
)

# Applied or beyond: in the pipeline, no longer a "today's lead".
PIPELINE_STATES: tuple[str, ...] = (
    "APPLIED",
    "REPLIED",
    "WON",
    "IN_PROGRESS",
    "COMPLETED",
    "PAID",
)

CLOSED_STATES: tuple[str, ...] = OFF_PATH_STATES

# Which state a lead naturally moves to next. Used for validation messages.
_NEXT: dict[str, tuple[str, ...]] = {
    "DISCOVERED": ("QUALIFIED", "SHORTLISTED", "READY_TO_APPLY", "REJECTED", "IGNORED", "EXPIRED"),
    "QUALIFIED": ("SHORTLISTED", "READY_TO_APPLY", "REJECTED", "IGNORED", "EXPIRED"),
    "SHORTLISTED": ("READY_TO_APPLY", "APPLIED", "REJECTED", "IGNORED", "EXPIRED"),
    "READY_TO_APPLY": ("APPLIED", "REJECTED", "IGNORED", "EXPIRED"),
    "APPLIED": ("REPLIED", "WON", "REJECTED", "EXPIRED"),
    "REPLIED": ("WON", "REJECTED", "EXPIRED"),
    "WON": ("IN_PROGRESS", "PAID", "REJECTED"),
    "IN_PROGRESS": ("COMPLETED", "REJECTED"),
    "COMPLETED": ("PAID",),
    "PAID": (),
    "REJECTED": ("DISCOVERED", "QUALIFIED"),
    "IGNORED": ("DISCOVERED", "QUALIFIED"),
    "EXPIRED": ("DISCOVERED", "QUALIFIED"),
}

from leadscore import CURRENCY_TO_USD

# One source of truth for the exchange rate. Deriving INR from the same table
# leadscore uses stops the two drifting apart (they previously disagreed by a
# fraction of a percent, enough to make debt progress never read 100%).
USD_TO_INR = 1.0 / CURRENCY_TO_USD["INR"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    lead_id           TEXT PRIMARY KEY,
    source            TEXT NOT NULL,
    title             TEXT NOT NULL,
    company           TEXT,
    url               TEXT,
    work_type         TEXT,
    opportunity_score INTEGER DEFAULT 0,
    priority          TEXT,
    skill_match_pct   INTEGER DEFAULT 0,
    interview         TEXT,
    estimated_hours   REAL DEFAULT 0,
    implied_hourly_usd REAL DEFAULT 0,
    budget_text       TEXT,
    budget_usd        REAL DEFAULT 0,
    competition       INTEGER,
    location          TEXT,
    posted_at         TEXT,
    discovered_at     TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    state             TEXT NOT NULL DEFAULT 'DISCOVERED',
    state_changed_at  TEXT NOT NULL,
    reason            TEXT,
    why               TEXT,
    red_flags         TEXT,
    matched_skills    TEXT,
    proposal          TEXT,
    actions_taken     INTEGER DEFAULT 0,
    source_payload    TEXT
);
CREATE INDEX IF NOT EXISTS idx_leads_state ON leads(state);
CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(opportunity_score DESC);
CREATE INDEX IF NOT EXISTS idx_leads_discovered ON leads(discovered_at DESC);

CREATE TABLE IF NOT EXISTS llm_cache (
    job_id       TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    analysis     TEXT NOT NULL,
    llm_score    INTEGER,
    llm_status   TEXT,
    checked_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_stats (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    at                 TEXT NOT NULL,
    raw                INTEGER DEFAULT 0,
    unique_count       INTEGER DEFAULT 0,
    rejected           INTEGER DEFAULT 0,
    screened           INTEGER DEFAULT 0,
    leads              INTEGER DEFAULT 0,
    deepseek_candidates INTEGER DEFAULT 0,
    deepseek_calls     INTEGER DEFAULT 0,
    deepseek_successes INTEGER DEFAULT 0,
    deepseek_failures  INTEGER DEFAULT 0,
    deepseek_skipped   INTEGER DEFAULT 0,
    deepseek_cached    INTEGER DEFAULT 0,
    proposal_calls     INTEGER DEFAULT 0,
    kind               TEXT DEFAULT 'run'
);

CREATE TABLE IF NOT EXISTS lead_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    TEXT NOT NULL,
    at         TEXT NOT NULL,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    note       TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_lead ON lead_events(lead_id);

CREATE TABLE IF NOT EXISTS revenue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     TEXT NOT NULL,
    at          TEXT NOT NULL,
    amount      REAL NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'USD',
    amount_usd  REAL NOT NULL,
    amount_inr  REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_revenue_lead ON revenue(lead_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return value or None


def to_usd(amount: float, currency: str) -> float:
    return amount * CURRENCY_TO_USD.get((currency or "USD").upper(), 1.0)


@dataclass
class FunnelMetrics:
    days: int
    discovered: int = 0
    qualified: int = 0
    shortlisted: int = 0
    applied: int = 0
    replied: int = 0
    won: int = 0
    completed: int = 0
    paid: int = 0
    rejected: int = 0
    ignored: int = 0
    expired: int = 0
    average_score: float = 0.0

    @property
    def reply_rate(self) -> float:
        return (self.replied / self.applied * 100) if self.applied else 0.0

    @property
    def win_rate(self) -> float:
        return (self.won / self.applied * 100) if self.applied else 0.0

    @property
    def completion_rate(self) -> float:
        return (self.completed / self.won * 100) if self.won else 0.0

    @property
    def payment_rate(self) -> float:
        return (self.paid / self.completed * 100) if self.completed else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "discovered": self.discovered,
            "qualified": self.qualified,
            "shortlisted": self.shortlisted,
            "applied": self.applied,
            "replied": self.replied,
            "won": self.won,
            "completed": self.completed,
            "paid": self.paid,
            "rejected": self.rejected,
            "ignored": self.ignored,
            "expired": self.expired,
            "average_score": round(self.average_score, 1),
            "reply_rate_pct": round(self.reply_rate, 1),
            "win_rate_pct": round(self.win_rate, 1),
            "completion_rate_pct": round(self.completion_rate, 1),
            "payment_rate_pct": round(self.payment_rate, 1),
        }


@dataclass
class RevenueSummary:
    won_usd: float = 0.0
    collected_usd: float = 0.0
    pending_usd: float = 0.0
    won_inr: float = 0.0
    collected_inr: float = 0.0
    pending_inr: float = 0.0
    debt_target_inr: float = 0.0
    debt_deadline: str | None = None
    days_remaining: int | None = None

    @property
    def debt_reduction_pct(self) -> float:
        if not self.debt_target_inr:
            return 0.0
        return min(100.0, self.collected_inr / self.debt_target_inr * 100)

    @property
    def remaining_inr(self) -> float:
        return max(0.0, self.debt_target_inr - self.collected_inr)

    def as_dict(self) -> dict[str, Any]:
        return {
            "won_usd": round(self.won_usd, 2),
            "collected_usd": round(self.collected_usd, 2),
            "pending_usd": round(self.pending_usd, 2),
            "won_inr": round(self.won_inr, 2),
            "collected_inr": round(self.collected_inr, 2),
            "pending_inr": round(self.pending_inr, 2),
            "debt_target_inr": round(self.debt_target_inr, 2),
            "debt_reduction_pct": round(self.debt_reduction_pct, 2),
            "remaining_inr": round(self.remaining_inr, 2),
            "debt_deadline": self.debt_deadline,
            "days_remaining": self.days_remaining,
        }


class LeadStore:
    """Thin, explicit wrapper around a SQLite database of leads."""

    # Columns added after the first release; applied to existing databases.
    _ADDED_COLUMNS = {
        "llm_score": "INTEGER",
        "llm_status": "TEXT",
        "llm_recommendation": "TEXT",
    }

    def __init__(self, path: str | Path = "leads.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    # Columns added to run_stats after the first release.
    # The complete expected shape. _add_missing_columns skips what already
    # exists, so listing every column makes the migration total: any older or
    # partial run_stats table is brought up to date rather than half-fixed.
    _ADDED_RUN_STATS_COLUMNS = {
        "raw": "INTEGER DEFAULT 0",
        "unique_count": "INTEGER DEFAULT 0",
        "rejected": "INTEGER DEFAULT 0",
        "screened": "INTEGER DEFAULT 0",
        "leads": "INTEGER DEFAULT 0",
        "deepseek_candidates": "INTEGER DEFAULT 0",
        "deepseek_calls": "INTEGER DEFAULT 0",
        "deepseek_successes": "INTEGER DEFAULT 0",
        "deepseek_failures": "INTEGER DEFAULT 0",
        "deepseek_skipped": "INTEGER DEFAULT 0",
        "deepseek_cached": "INTEGER DEFAULT 0",
        "proposal_calls": "INTEGER DEFAULT 0",
        "kind": "TEXT DEFAULT 'run'",
    }

    def _migrate(self) -> None:
        """Add later columns to a database created by an earlier version."""
        self._add_missing_columns("leads", self._ADDED_COLUMNS)
        self._add_missing_columns("run_stats", self._ADDED_RUN_STATS_COLUMNS)

    def _add_missing_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        for column, kind in columns.items():
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "LeadStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- leads -------------------------------------------------------------

    def upsert_lead(self, record: dict[str, Any], state: str = "DISCOVERED") -> str:
        """Insert or refresh a lead. Returns 'new', 'rescored' or 'unchanged'.

        A lead that already exists is never duplicated. Its score is refreshed
        so improved scoring logic can lift an old lead, but its lifecycle state
        and proposal are preserved: human decisions are never overwritten."""
        lead_id = str(record["lead_id"])
        existing = self.get_lead(lead_id)
        now = _now()

        if existing is None:
            self.conn.execute(
                """
                INSERT INTO leads (
                    lead_id, source, title, company, url, work_type,
                    opportunity_score, priority, skill_match_pct, interview,
                    estimated_hours, implied_hourly_usd, budget_text, budget_usd,
                    competition, location, posted_at, discovered_at, updated_at,
                    state, state_changed_at, reason, why, red_flags,
                    matched_skills, source_payload,
                    llm_score, llm_status, llm_recommendation
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    lead_id,
                    record.get("source", ""),
                    record.get("title", ""),
                    record.get("company", ""),
                    record.get("url", ""),
                    record.get("work_type", ""),
                    int(record.get("opportunity_score", 0)),
                    record.get("priority", ""),
                    int(record.get("skill_match_pct", 0)),
                    record.get("interview", ""),
                    float(record.get("estimated_hours", 0) or 0),
                    float(record.get("implied_hourly_usd", 0) or 0),
                    record.get("budget_text", ""),
                    float(record.get("budget_usd", 0) or 0),
                    record.get("competition"),
                    record.get("location", ""),
                    _iso(record.get("posted_at")),
                    record.get("discovered_at") or now,
                    now,
                    state,
                    now,
                    record.get("reason", ""),
                    record.get("why", ""),
                    json.dumps(record.get("red_flags", [])),
                    json.dumps(record.get("matched_skills", [])),
                    json.dumps(record.get("source_payload", {})),
                    record.get("llm_score"),
                    record.get("llm_status", ""),
                    record.get("llm_recommendation", ""),
                ),
            )
            self._event(lead_id, None, state, "discovered")
            self.conn.commit()
            return "new"

        old_score = int(existing["opportunity_score"] or 0)
        new_score = int(record.get("opportunity_score", 0))
        self.conn.execute(
            """
            UPDATE leads SET
                opportunity_score = ?, priority = ?, skill_match_pct = ?,
                interview = ?, estimated_hours = ?, implied_hourly_usd = ?,
                budget_text = ?, budget_usd = ?, competition = ?, reason = ?,
                why = ?, red_flags = ?, matched_skills = ?, updated_at = ?,
                source_payload = ?
            WHERE lead_id = ?
            """,
            (
                new_score,
                record.get("priority", existing["priority"]),
                int(record.get("skill_match_pct", 0)),
                record.get("interview", existing["interview"]),
                float(record.get("estimated_hours", 0) or 0),
                float(record.get("implied_hourly_usd", 0) or 0),
                record.get("budget_text", ""),
                float(record.get("budget_usd", 0) or 0),
                record.get("competition"),
                record.get("reason", ""),
                record.get("why", ""),
                json.dumps(record.get("red_flags", [])),
                json.dumps(record.get("matched_skills", [])),
                now,
                json.dumps(record.get("source_payload", {})),
                lead_id,
            ),
        )
        self.conn.commit()
        return "rescored" if new_score != old_score else "unchanged"

    def get_lead(self, lead_id: str) -> sqlite3.Row | None:
        cur = self.conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,))
        return cur.fetchone()

    def set_state(self, lead_id: str, state: str, note: str = "") -> bool:
        """Move a lead through the lifecycle, recording an event."""
        state = state.upper()
        if state not in ALL_STATES:
            raise ValueError(f"unknown state: {state}")
        row = self.get_lead(lead_id)
        if row is None:
            return False
        now = _now()
        self.conn.execute(
            "UPDATE leads SET state = ?, state_changed_at = ?, updated_at = ? WHERE lead_id = ?",
            (state, now, now, lead_id),
        )
        if state == "APPLIED":
            self.conn.execute(
                "UPDATE leads SET actions_taken = actions_taken + 1 WHERE lead_id = ?",
                (lead_id,),
            )
        self._event(lead_id, row["state"], state, note)
        self.conn.commit()
        return True

    def set_proposal(self, lead_id: str, proposal: str) -> bool:
        cur = self.conn.execute(
            "UPDATE leads SET proposal = ?, updated_at = ? WHERE lead_id = ?",
            (proposal, _now(), lead_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def _event(self, lead_id: str, from_state: str | None, to_state: str, note: str) -> None:
        self.conn.execute(
            "INSERT INTO lead_events (lead_id, at, from_state, to_state, note) VALUES (?,?,?,?,?)",
            (lead_id, _now(), from_state, to_state, note or ""),
        )

    def events(self, lead_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM lead_events WHERE lead_id = ? ORDER BY id", (lead_id,)
            )
        )

    def top_leads(
        self,
        limit: int = 10,
        min_score: int = 0,
        states: Iterable[str] = OPEN_STATES,
        since: str | None = None,
    ) -> list[sqlite3.Row]:
        """Best open leads, highest score first. Never returns the same lead twice.

        ``since`` (an ISO timestamp) restricts to leads discovered in a window,
        which is how the dashboard shows *today's* leads rather than repeating
        the same opportunity every day."""
        states = tuple(states)
        placeholders = ",".join("?" for _ in states)
        sql = (
            f"SELECT * FROM leads WHERE state IN ({placeholders}) "
            "AND opportunity_score >= ? "
        )
        params: list[Any] = [*states, min_score]
        if since:
            sql += "AND discovered_at >= ? "
            params.append(since)
        sql += "ORDER BY opportunity_score DESC, discovered_at DESC LIMIT ?"
        params.append(limit)
        return list(self.conn.execute(sql, tuple(params)))

    def known_lead_ids(self) -> set[str]:
        """Every lead already seen.

        The funnel uses this to avoid re-scoring and, more importantly,
        re-emailing an opportunity that has already been handled."""
        return {
            row["lead_id"]
            for row in self.conn.execute("SELECT lead_id FROM leads")
        }

    def pipeline(self) -> list[sqlite3.Row]:
        placeholders = ",".join("?" for _ in PIPELINE_STATES)
        return list(
            self.conn.execute(
                f"SELECT * FROM leads WHERE state IN ({placeholders}) "
                "ORDER BY state_changed_at DESC",
                PIPELINE_STATES,
            )
        )

    def count_by_state(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT state, COUNT(*) AS n FROM leads GROUP BY state"
        )
        return {row["state"]: row["n"] for row in rows}

    def mark_stale_as_expired(self, max_age_days: int = 45) -> int:
        """Expire open leads that are too old to still be worth showing."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        rows = list(
            self.conn.execute(
                "SELECT lead_id FROM leads WHERE state IN "
                f"({','.join('?' for _ in OPEN_STATES)}) AND discovered_at < ?",
                (*OPEN_STATES, cutoff),
            )
        )
        for row in rows:
            self.set_state(row["lead_id"], "EXPIRED", f"older than {max_age_days} days")
        return len(rows)

    # -- DeepSeek analysis cache -------------------------------------------

    def get_llm_analysis(self, job_id: str, content_hash: str) -> dict | None:
        """Return a cached analysis, or None when it is missing or stale.

        A stale entry (the listing materially changed) is treated as a miss so
        it gets re-analysed exactly once."""
        row = self.conn.execute(
            "SELECT content_hash, analysis, llm_status FROM llm_cache WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None or row["content_hash"] != content_hash:
            return None
        try:
            analysis = json.loads(row["analysis"])
        except (TypeError, ValueError):
            return None
        if not isinstance(analysis, dict) or not analysis:
            return None
        analysis["_cached"] = True
        analysis["_status"] = row["llm_status"]
        return analysis

    def save_llm_analysis(
        self,
        job_id: str,
        content_hash: str,
        analysis: dict,
        status: str = "ok",
    ) -> None:
        """Persist one analysis so the same listing is never analysed twice."""
        self.conn.execute(
            "INSERT OR REPLACE INTO llm_cache "
            "(job_id, content_hash, analysis, llm_score, llm_status, checked_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                job_id,
                content_hash,
                json.dumps(analysis),
                analysis.get("llm_score"),
                status,
                _now(),
            ),
        )
        self.conn.commit()

    def llm_cache_size(self) -> int:
        return int(
            self.conn.execute("SELECT COUNT(*) c FROM llm_cache").fetchone()["c"]
        )

    # -- per-run usage ------------------------------------------------------

    def record_run_stats(self, stats: dict[str, Any], kind: str = "run") -> None:
        """Append one run's funnel and DeepSeek-usage counters.

        This is what makes "is DeepSeek usage staying low?" answerable."""
        self.conn.execute(
            """
            INSERT INTO run_stats (
                at, raw, unique_count, rejected, screened, leads,
                deepseek_candidates, deepseek_calls, deepseek_successes,
                deepseek_failures, deepseek_skipped, deepseek_cached,
                proposal_calls, kind
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                _now(),
                int(stats.get("raw", 0) or 0),
                int(stats.get("unique", 0) or 0),
                int(stats.get("rejected", 0) or 0),
                int(stats.get("screened", 0) or 0),
                int(stats.get("leads", 0) or 0),
                int(stats.get("deepseek_candidates", 0) or 0),
                int(stats.get("deepseek_calls", 0) or 0),
                int(stats.get("deepseek_successes", 0) or 0),
                int(stats.get("deepseek_failures", 0) or 0),
                int(stats.get("deepseek_skipped", 0) or 0),
                int(stats.get("deepseek_cached", 0) or 0),
                int(stats.get("proposal_calls", 0) or 0),
                kind,
            ),
        )
        self.conn.commit()

    def recent_run_stats(self, limit: int = 10, kind: str | None = None) -> list[sqlite3.Row]:
        if kind is None:
            return list(
                self.conn.execute(
                    "SELECT * FROM run_stats ORDER BY id DESC LIMIT ?", (limit,)
                )
            )
        return list(
            self.conn.execute(
                "SELECT * FROM run_stats WHERE kind = ? ORDER BY id DESC LIMIT ?",
                (kind, limit),
            )
        )

    def llm_usage_summary(self, runs: int = 30) -> dict[str, Any]:
        """DeepSeek usage for the most recent run and across recent runs."""
        rows = self.recent_run_stats(limit=runs)
        run_rows = self.recent_run_stats(limit=runs, kind="run")
        keys = (
            "deepseek_calls",
            "deepseek_successes",
            "deepseek_failures",
            "deepseek_skipped",
            "deepseek_cached",
            "proposal_calls",
        )
        totals = {
            key: sum(
                int(row[key] or 0) for row in rows if key in row.keys()
            )
            for key in keys
        }
        last = dict(run_rows[0]) if run_rows else {}
        return {
            "runs": len(rows),
            "cache_entries": self.llm_cache_size(),
            "last_run": last,
            "totals": totals,
        }

    # -- revenue -----------------------------------------------------------

    def record_revenue(
        self,
        lead_id: str,
        amount: float,
        currency: str = "USD",
        status: str = "pending",
        note: str = "",
    ) -> None:
        status = status.lower()
        if status not in ("pending", "collected"):
            raise ValueError("status must be 'pending' or 'collected'")
        usd = to_usd(amount, currency)
        self.conn.execute(
            "INSERT INTO revenue (lead_id, at, amount, currency, amount_usd, amount_inr, status, note)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (lead_id, _now(), amount, currency.upper(), usd, usd * USD_TO_INR, status, note),
        )
        self.conn.commit()

    def revenue_summary(
        self, debt_target_inr: float = 0.0, debt_deadline: str | None = None
    ) -> RevenueSummary:
        summary = RevenueSummary(debt_target_inr=debt_target_inr, debt_deadline=debt_deadline)
        rows = self.conn.execute(
            "SELECT status, SUM(amount_usd) u, SUM(amount_inr) i FROM revenue GROUP BY status"
        )
        for row in rows:
            if row["status"] == "collected":
                summary.collected_usd = row["u"] or 0.0
                summary.collected_inr = row["i"] or 0.0
            else:
                summary.pending_usd = row["u"] or 0.0
                summary.pending_inr = row["i"] or 0.0
        summary.won_usd = summary.collected_usd + summary.pending_usd
        summary.won_inr = summary.collected_inr + summary.pending_inr
        if debt_deadline:
            try:
                deadline = datetime.fromisoformat(debt_deadline).replace(tzinfo=timezone.utc)
                summary.days_remaining = max(
                    0, (deadline - datetime.now(timezone.utc)).days
                )
            except ValueError:
                summary.days_remaining = None
        return summary

    # -- metrics -----------------------------------------------------------

    def funnel_metrics(self, days: int = 30) -> FunnelMetrics:
        """Conversion KPIs over a window. The KPI that matters is paid tasks."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        metrics = FunnelMetrics(days=days)

        row = self.conn.execute(
            "SELECT COUNT(*) n, AVG(opportunity_score) a FROM leads WHERE discovered_at >= ?",
            (cutoff,),
        ).fetchone()
        metrics.discovered = row["n"] or 0
        metrics.average_score = row["a"] or 0.0

        rows = self.conn.execute(
            "SELECT to_state, COUNT(DISTINCT lead_id) n FROM lead_events "
            "WHERE at >= ? GROUP BY to_state",
            (cutoff,),
        )
        counts = {r["to_state"]: r["n"] for r in rows}
        metrics.qualified = counts.get("QUALIFIED", 0)
        metrics.shortlisted = counts.get("SHORTLISTED", 0)
        metrics.applied = counts.get("APPLIED", 0)
        metrics.replied = counts.get("REPLIED", 0)
        metrics.won = counts.get("WON", 0)
        metrics.completed = counts.get("COMPLETED", 0)
        metrics.paid = counts.get("PAID", 0)
        metrics.rejected = counts.get("REJECTED", 0)
        metrics.ignored = counts.get("IGNORED", 0)
        metrics.expired = counts.get("EXPIRED", 0)
        return metrics

    # -- migration ---------------------------------------------------------

    def import_legacy_csv(self, csv_path: str | Path) -> int:
        """Bring historical jobs_log.csv rows in as IGNORED leads.

        Only rows that were actually emailed are imported. Those must never be
        re-sent. Rows the old pipeline merely screened are deliberately skipped:
        they were judged by a different, job-oriented rubric, and treating them
        as "already handled" would permanently block listings that the current
        task-oriented scoring would rate highly.

        Rejected rows are skipped for the same reason — the CSV is also an audit
        trail, and an audit trail must not become a blacklist."""
        path = Path(csv_path)
        if not path.exists():
            return 0

        imported = 0
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                lead_id = (row.get("job_id") or "").strip()
                if not lead_id or self.get_lead(lead_id):
                    continue
                if str(row.get("emailed", "")).strip().lower() != "true":
                    continue
                if (row.get("reason") or "").startswith("rejected:"):
                    continue
                try:
                    raw_score = float(row.get("score") or 0)
                except ValueError:
                    raw_score = 0.0
                # jobs_log.csv has held two scales: the original pipeline wrote
                # 1-10, the task pipeline writes 0-100. Detect rather than
                # assume, otherwise every legacy row imports as 100.
                score = int(raw_score) if raw_score > 10 else int(raw_score * 10)
                now = _now()
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO leads (
                        lead_id, source, title, company, url, work_type,
                        opportunity_score, priority, skill_match_pct, interview,
                        estimated_hours, implied_hourly_usd, budget_text, budget_usd,
                        competition, location, posted_at, discovered_at, updated_at,
                        state, state_changed_at, reason, why, red_flags,
                        matched_skills, source_payload
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        lead_id,
                        row.get("source", ""),
                        row.get("title", ""),
                        row.get("company", ""),
                        row.get("url", ""),
                        "unknown",
                        min(100, score),
                        "LEGACY",
                        0,
                        "UNKNOWN",
                        0.0,
                        0.0,
                        "",
                        0.0,
                        None,
                        "",
                        row.get("timestamp"),
                        row.get("timestamp") or now,
                        now,
                        "IGNORED",
                        now,
                        row.get("reason", ""),
                        "imported from jobs_log.csv (pre-task-pipeline)",
                        "[]",
                        "[]",
                        "{}",
                    ),
                )
                self._event(lead_id, None, "IGNORED", "legacy import")
                imported += 1
        self.conn.commit()
        return imported
