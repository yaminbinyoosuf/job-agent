"""Rendering for the daily lead digest (email) and the HTML dashboard.

The primary screen is "TODAY'S BEST PAID TASKS". It shows a deliberately small
number of leads: quality over quantity. If only three clear the bar, it shows
three and says so rather than padding the list.

Output is self-contained — no CDN, no external fonts — so the HTML file works
offline and can be committed by the scheduled workflow.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

RULE = "\u2501" * 54


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def relative_time(value: Any, now: datetime | None = None) -> str:
    if not value:
        return "unknown"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return "unknown"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    seconds = max(0, (now - value).total_seconds())
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(seconds // 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"


def format_duration(hours: float) -> str:
    if not hours:
        return "unknown"
    if hours <= 6:
        return "a few hours"
    if hours <= 12:
        return "about 1 day"
    if hours <= 40:
        return f"{max(1, round(hours / 8))} days"
    return f"~{round(hours / 40)} week(s)"


def format_money(lead: Any) -> str:
    budget_text = _get(lead, "budget_text") or ""
    budget_usd = float(_get(lead, "budget_usd") or 0)
    work_type = (_get(lead, "work_type") or "").lower()
    if not budget_text or budget_text == "not stated":
        return "not stated"
    suffix = " fixed" if work_type == "task" and "fixed" not in budget_text.lower() else ""
    if "/hr" in budget_text or "hourly" in budget_text:
        return budget_text
    return f"{budget_text}{suffix}"


def format_competition(lead: Any) -> str:
    value = _get(lead, "competition")
    if value is None:
        return "unknown"
    value = int(value)
    return f"{value} proposal{'s' if value != 1 else ''}"


def _get(lead: Any, key: str, default: Any = None) -> Any:
    """Read a field from either a sqlite3.Row or a plain dict."""
    try:
        if isinstance(lead, dict):
            return lead.get(key, default)
        return lead[key] if key in lead.keys() else default
    except (TypeError, IndexError, KeyError):
        return default


def _json_list(lead: Any, key: str) -> list[str]:
    raw = _get(lead, key)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def recommended_action(lead: Any) -> str:
    state = (_get(lead, "state") or "DISCOVERED").upper()
    score = int(_get(lead, "opportunity_score") or 0)
    if state in ("APPLIED", "REPLIED", "WON", "IN_PROGRESS", "COMPLETED", "PAID"):
        return {"APPLIED": "FOLLOW UP", "REPLIED": "REPLY", "WON": "START WORK",
                "IN_PROGRESS": "CONTINUE", "COMPLETED": "INVOICE", "PAID": "DONE"}.get(state, "REVIEW")
    if state in ("REJECTED", "IGNORED", "EXPIRED"):
        return "ARCHIVED"
    if score >= 80:
        return "APPLY NOW"
    if score >= 65:
        return "APPLY"
    if score >= 50:
        return "REVIEW"
    return "SKIP"


# ---------------------------------------------------------------------------
# Text digest
# ---------------------------------------------------------------------------


def render_lead_block(lead: Any, now: datetime | None = None) -> str:
    score = int(_get(lead, "opportunity_score") or 0)
    priority = _get(lead, "priority") or "LOW"
    why = _get(lead, "why") or _get(lead, "reason") or "no summary available"
    red_flags = _json_list(lead, "red_flags")
    lines = [
        RULE,
        f"{score}/100 \u2014 {priority}",
        "",
        str(_get(lead, "title") or "Untitled"),
        "",
        f"Platform:    {_get(lead, 'source') or 'unknown'}",
        f"Budget:      {format_money(lead)}",
        f"Duration:    {format_duration(float(_get(lead, 'estimated_hours') or 0))}",
        f"Match:       {int(_get(lead, 'skill_match_pct') or 0)}%",
        f"Interview:   {_get(lead, 'interview') or 'unknown'}",
        f"Competition: {format_competition(lead)}",
        f"Posted:      {relative_time(_get(lead, 'posted_at'), now)}",
        f"URL:         {_get(lead, 'url') or ''}",
        "",
        "WHY:",
        why,
    ]
    if red_flags:
        lines += ["", "RED FLAGS:", *[f"- {flag}" for flag in red_flags]]
    lines += ["", "ACTION:", recommended_action(lead), RULE]
    return "\n".join(lines)


def render_digest(
    leads: Sequence[Any],
    metrics: Any = None,
    revenue: Any = None,
    *,
    now: datetime | None = None,
) -> str:
    """Plain-text daily digest, used as the email body."""
    now = now or datetime.now(timezone.utc)
    lines = ["TODAY'S PAID TASK LEADS", "=" * 54, ""]

    if not leads:
        lines.append("No leads cleared the quality bar today.")
        lines.append("This is intentional: the bar is not lowered to fill the list.")
    else:
        for index, lead in enumerate(leads, 1):
            score = int(_get(lead, "opportunity_score") or 0)
            priority = _get(lead, "priority") or ""
            budget = format_money(lead)
            title = str(_get(lead, "title") or "")[:58]
            lines.append(f"{index}. {priority} \u2014 {budget} \u2014 {title} (score {score})")
        lines.append("")
        lines.append(RULE)
        lines.append("")
        for lead in leads:
            lines.append(render_lead_block(lead, now))
            lines.append("")

    gross = sum(float(_get(lead, "budget_usd") or 0) for lead in leads)
    average = (
        sum(int(_get(lead, "opportunity_score") or 0) for lead in leads) / len(leads)
        if leads
        else 0.0
    )
    lines += [
        "",
        "=" * 54,
        "SUMMARY",
        "=" * 54,
        f"Top opportunities shown: {len(leads)}",
        f"Average score:           {average:.0f}",
        f"Potential gross value:   ${gross:,.0f}",
    ]
    if metrics is not None:
        data = metrics.as_dict() if hasattr(metrics, "as_dict") else dict(metrics)
        lines += [
            f"Discovered ({data.get('days', 30)}d):      {data.get('discovered', 0)}",
            f"Qualified:               {data.get('qualified', 0)}",
            f"Applied:                 {data.get('applied', 0)}",
            f"Replied:                 {data.get('replied', 0)}  "
            f"(reply rate {data.get('reply_rate_pct', 0)}%)",
            f"Won:                     {data.get('won', 0)}",
            f"Paid:                    {data.get('paid', 0)}",
            "",
            "KPI that matters: paid tasks won, not listings scraped.",
        ]
    if revenue is not None:
        data = revenue.as_dict() if hasattr(revenue, "as_dict") else dict(revenue)
        if data.get("debt_target_inr"):
            lines += [
                "",
                f"Revenue collected:  ${data.get('collected_usd', 0):,.0f} "
                f"(\u20b9{data.get('collected_inr', 0):,.0f})",
                f"Revenue pending:    ${data.get('pending_usd', 0):,.0f} "
                f"(\u20b9{data.get('pending_inr', 0):,.0f})",
                f"Debt target:        \u20b9{data.get('debt_target_inr', 0):,.0f}"
                + (f" by {data.get('debt_deadline')}" if data.get("debt_deadline") else ""),
                f"Debt reduced:       {data.get('debt_reduction_pct', 0)}%  "
                f"(\u20b9{data.get('remaining_inr', 0):,.0f} remaining)",
            ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------

_CSS = """
:root{--bg:#0f1115;--card:#171a21;--line:#252a34;--fg:#e6e9ef;--dim:#9aa4b2;
--high:#22c55e;--med:#eab308;--low:#64748b;--accent:#38bdf8;--warn:#f87171}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
header{padding:28px 24px 18px;border-bottom:1px solid var(--line)}
h1{margin:0;font-size:22px;letter-spacing:.2px}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.09em;color:var(--dim);
margin:34px 24px 12px}
.sub{color:var(--dim);font-size:13px;margin-top:6px}
.wrap{max-width:1080px;margin:0 auto;padding:0 24px 60px}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin:18px 24px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:10px 14px;min-width:132px}
.stat .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.07em}
.stat .v{font-size:19px;font-weight:600;margin-top:3px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:18px;margin:0 0 14px}
.card.high{border-left:4px solid var(--high)}
.card.medhigh{border-left:4px solid var(--med)}
.card.low{border-left:4px solid var(--low)}
.top{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}
.score{font-size:26px;font-weight:700;line-height:1}
.badge{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.06em;
padding:3px 8px;border-radius:20px;text-transform:uppercase}
.b-high{background:rgba(34,197,94,.16);color:var(--high)}
.b-med{background:rgba(234,179,8,.16);color:var(--med)}
.b-low{background:rgba(100,116,139,.2);color:var(--dim)}
.title{font-size:17px;font-weight:600;margin:2px 0 10px}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));
gap:8px 18px;margin:12px 0}
.meta div{font-size:13px;color:var(--dim)}
.meta b{color:var(--fg);font-weight:600}
.why{margin-top:12px;padding-top:12px;border-top:1px solid var(--line);font-size:14px}
.lbl{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.07em;
display:block;margin-bottom:4px}
.flags{color:var(--warn);font-size:13px;margin-top:8px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.action{display:inline-block;margin-top:14px;background:var(--accent);color:#04121c;
font-weight:700;font-size:12px;letter-spacing:.06em;padding:7px 14px;border-radius:8px}
.action.ghost{background:transparent;color:var(--dim);border:1px solid var(--line)}
pre{white-space:pre-wrap;background:#11141a;border:1px solid var(--line);
border-radius:8px;padding:12px;font-size:12.5px;color:var(--dim);margin:10px 0 0}
.empty{color:var(--dim);padding:26px;border:1px dashed var(--line);border-radius:12px;
text-align:center}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:600;font-size:11px;text-transform:uppercase;
letter-spacing:.07em}
"""


def _escape(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _priority_class(priority: str, score: int) -> str:
    if score >= 75 or priority in ("HIGH PRIORITY", "HIGH"):
        return "high"
    if score >= 50 or priority == "MEDIUM-HIGH":
        return "medhigh"
    return "low"


def _badge_class(score: int) -> str:
    if score >= 75:
        return "b-high"
    if score >= 50:
        return "b-med"
    return "b-low"


def _lead_card(lead: Any, now: datetime) -> str:
    score = int(_get(lead, "opportunity_score") or 0)
    priority = _get(lead, "priority") or ""
    cls = _priority_class(priority, score)
    red_flags = _json_list(lead, "red_flags")
    matched = _json_list(lead, "matched_skills")
    url = _get(lead, "url") or ""
    proposal = _get(lead, "proposal") or ""

    meta_rows = [
        ("Platform", _get(lead, "source")),
        ("Budget", format_money(lead)),
        ("Duration", format_duration(float(_get(lead, "estimated_hours") or 0))),
        ("Skill match", f"{int(_get(lead, 'skill_match_pct') or 0)}%"),
        ("Interview", _get(lead, "interview")),
        ("Competition", format_competition(lead)),
        ("Posted", relative_time(_get(lead, "posted_at"), now)),
        ("State", _get(lead, "state")),
    ]
    meta_html = "".join(
        f"<div><span class='lbl'>{_escape(k)}</span><b>{_escape(v)}</b></div>"
        for k, v in meta_rows
    )
    flags_html = (
        f"<div class='flags'><b>Red flags:</b> {_escape('; '.join(red_flags))}</div>"
        if red_flags
        else ""
    )
    skills_html = (
        f"<div class='flags' style='color:var(--dim)'><b>Matched:</b> "
        f"{_escape(', '.join(matched[:8]))}</div>"
        if matched
        else ""
    )
    proposal_html = (
        f"<details><summary style='color:var(--dim);cursor:pointer;margin-top:10px;"
        f"font-size:13px'>Proposal draft</summary><pre>{_escape(proposal)}</pre></details>"
        if proposal
        else ""
    )
    link_html = (
        f"<a href='{_escape(url)}' target='_blank' rel='noopener'>Open original listing</a>"
        if url
        else ""
    )

    return f"""<div class="card {cls}">
  <div class="top">
    <div><span class="score">{score}</span><span style="color:var(--dim)">/100</span></div>
    <span class="badge {_badge_class(score)}">{_escape(priority)}</span>
  </div>
  <div class="title">{_escape(_get(lead, 'title'))}</div>
  <div class="meta">{meta_html}</div>
  <div class="why"><span class="lbl">Why it matches</span>{_escape(_get(lead, 'why') or _get(lead, 'reason'))}</div>
  {skills_html}{flags_html}
  <div style="margin-top:14px">{link_html}</div>
  <span class="action">{_escape(recommended_action(lead))}</span>
  {proposal_html}
</div>"""


def render_dashboard_html(
    leads: Sequence[Any],
    metrics: Any = None,
    revenue: Any = None,
    pipeline: Sequence[Any] = (),
    *,
    now: datetime | None = None,
    generated_at: datetime | None = None,
) -> str:
    """Render the self-contained dashboard HTML."""
    now = now or datetime.now(timezone.utc)
    generated_at = generated_at or now
    gross = sum(float(_get(lead, "budget_usd") or 0) for lead in leads)
    average = (
        sum(int(_get(lead, "opportunity_score") or 0) for lead in leads) / len(leads)
        if leads
        else 0.0
    )

    stats = [
        ("Leads today", len(leads)),
        ("Average score", f"{average:.0f}"),
        ("Potential value", f"${gross:,.0f}"),
    ]
    if metrics is not None:
        data = metrics.as_dict() if hasattr(metrics, "as_dict") else dict(metrics)
        stats += [
            ("Applied", data.get("applied", 0)),
            ("Replied", data.get("replied", 0)),
            ("Won", data.get("won", 0)),
            ("Paid", data.get("paid", 0)),
        ]
    if revenue is not None:
        data = revenue.as_dict() if hasattr(revenue, "as_dict") else dict(revenue)
        stats.append(("Collected", f"${data.get('collected_usd', 0):,.0f}"))
        if data.get("debt_target_inr"):
            stats.append(("Debt reduced", f"{data.get('debt_reduction_pct', 0)}%"))

    stats_html = "".join(
        f"<div class='stat'><div class='k'>{_escape(k)}</div><div class='v'>{_escape(v)}</div></div>"
        for k, v in stats
    )

    if leads:
        cards = "\n".join(_lead_card(lead, now) for lead in leads)
        lead_section = f"<h2>Today's best paid tasks</h2><div class='wrap'>{cards}</div>"
    else:
        lead_section = (
            "<h2>Today's best paid tasks</h2><div class='wrap'>"
            "<div class='empty'>No leads cleared the quality bar today.<br>"
            "The bar is not lowered to fill the list.</div></div>"
        )

    pipeline_html = ""
    if pipeline:
        rows = "".join(
            "<tr>"
            f"<td>{_escape(_get(row, 'state'))}</td>"
            f"<td>{_escape(_get(row, 'title'))}</td>"
            f"<td>{_escape(_get(row, 'source'))}</td>"
            f"<td>{_escape(format_money(row))}</td>"
            f"<td>{int(_get(row, 'opportunity_score') or 0)}</td>"
            f"<td>{_escape(relative_time(_get(row, 'state_changed_at'), now))}</td>"
            "</tr>"
            for row in pipeline
        )
        pipeline_html = (
            "<h2>Pipeline (applied and beyond)</h2><div class='wrap'><table>"
            "<tr><th>State</th><th>Task</th><th>Platform</th><th>Budget</th>"
            f"<th>Score</th><th>Moved</th></tr>{rows}</table></div>"
        )

    revenue_html = ""
    if revenue is not None:
        data = revenue.as_dict() if hasattr(revenue, "as_dict") else dict(revenue)
        revenue_html = (
            "<h2>Revenue and debt</h2><div class='wrap'><table>"
            f"<tr><th>Won</th><td>${data.get('won_usd', 0):,.0f}</td></tr>"
            f"<tr><th>Collected</th><td>${data.get('collected_usd', 0):,.0f} "
            f"(\u20b9{data.get('collected_inr', 0):,.0f})</td></tr>"
            f"<tr><th>Pending</th><td>${data.get('pending_usd', 0):,.0f}</td></tr>"
            f"<tr><th>Debt target</th><td>\u20b9{data.get('debt_target_inr', 0):,.0f}"
            + (f" by {_escape(data.get('debt_deadline'))}" if data.get("debt_deadline") else "")
            + "</td></tr>"
            f"<tr><th>Remaining</th><td>\u20b9{data.get('remaining_inr', 0):,.0f}</td></tr>"
            "</table></div>"
        )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Today's Best Paid Tasks</title><style>{_CSS}</style></head>
<body>
<header>
  <h1>Today's Best Paid Tasks</h1>
  <div class="sub">Paid, short, task-based engineering work &middot; generated
  {_escape(generated_at.strftime('%Y-%m-%d %H:%M UTC'))}</div>
</header>
<div class="stats">{stats_html}</div>
{lead_section}
{pipeline_html}
{revenue_html}
</body></html>"""


def write_dashboard(path: str | Path, html_text: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html_text, encoding="utf-8")
    return target
