# Paid Task Finder

An agent that finds a **small number of realistic, paid, short engineering tasks**
that Yamin can win and finish quickly — and deliberately ignores everything else.

It is not a job board and not a job-search engine. It does not want full-time
roles, permanent employment, long contracts, internships, unpaid trials, or
vague "AI Engineer" listings with no deliverable. It wants fixed-price tasks,
one-time technical work, small projects, bug fixes, integrations, scrapers,
automation and backend work with a clear, paid deliverable.

**Success is measured in paid tasks won, not listings scraped.**

---

## The funnel

```
raw listings  ->  deduplicate  ->  hard rejection filters  ->  skill match
      ->  payment/quality analysis  ->  opportunity score  ->  top 5-10 leads/day
```

The model is consulted **after** the cheap deterministic filters and only for
the best candidates, so cost tracks promise rather than raw volume.

A real offline run:

```
774 raw -> 559 unique -> 540 rejected -> 14 screened -> 8 leads
rejections: blocked_title=237  no_keyword_match=207  full_time=77
            no_deliverable=31  long_term=28  budget_too_low=13  scam=2
```

If only three leads clear the bar, three are shown. The list is never padded.

---

## Architecture

The system was already a working single-file agent; this builds on it rather
than replacing it. `job_agent.py` remains the orchestrator (discovery,
de-duplication, delivery, CLI). The new concerns live in focused modules so
they can be tested without network access.

| File | Responsibility |
| --- | --- |
| `job_agent.py` | Orchestrator: fetchers, delivery, CLI, scheduling entry point |
| `leadscore.py` | Pure scoring engine: hard rejection rules, skill match, budget/hours parsing, opportunity score |
| `pipeline.py` | The funnel itself: dedupe -> filters -> budgeted model pass -> threshold -> top N |
| `leadstore.py` | SQLite: lead lifecycle, events, revenue, funnel metrics |
| `dashboard.py` | "Today's Best Paid Tasks" HTML + the plain-text daily digest |
| `proposals.py` | Grounded proposal drafting (never invents experience) |
| `tests/` | 144 unit and end-to-end tests (stdlib `unittest`, no new deps) |

**Reused unchanged:** all eleven fetchers, the de-duplication logic, the
keyword/title blocklist, DeepSeek + Gemini scoring, Resend delivery, the
`jobs_log.csv` audit trail, and the GitHub Actions schedule.

---

## Discovery

Eleven sources. Three carry task-based work; the rest are conventional boards
that mostly supply full-time roles the hard filters correctly reject.

**Task-based:** **Mercor** (paid AI-training tasks; its board publishes real
interview metadata and location restrictions), **Freelancer.com** (public
projects API — bid and start), and the monthly **HN "Freelancer? Seeking
freelancer?"** thread.

**Boards:** RemoteOK, WeWorkRemotely, Remotive, Arbeitnow, Jobicy, Himalayas,
Working Nomads, HN "Who is hiring?".

**Sources were considered and rejected** for failing the "reliable public
API/URL" bar: Upwork (feed returns HTTP 410), Algora (returns zero items for
every query shape), IssueHunt, Polar, Guru (403), PeoplePerHour, Contra,
Mercor's private API, Outlier and DataAnnotation. No source was added merely to
inflate volume, and no listing, price or client is ever invented.

---

## Hard rejection filters

Applied before any model call. Any hit drops the listing.

| # | Rule | # | Rule |
| --- | --- | --- | --- |
| 1 | Full-time employment | 11 | Budget hidden when required (`REQUIRE_BUDGET`) |
| 2 | Internship / trainee | 12 | Location excludes India |
| 3 | Unpaid | 13 | Qualifications clearly not held (PhD, clearance, 8+ yrs) |
| 4 | Volunteer | 14 | Primarily a recruitment process |
| 5 | Contract-to-hire / temp-to-perm | 15 | Commission-only / revenue-share-only / equity-only |
| 6 | Long-term / ongoing / "months of work" | 16 | Scam signals (fees, wire transfer, unpaid trial) |
| 7 | 20+ hrs/week unless task-based *and* paid | 17 | Budget too low for the work (`MIN_HOURLY_VALUE`) |
| 8 | No concrete deliverable | 18 | Generic job with no actionable scope |
| 9 | Title blocklist (sales, marketing, hardware, senior/staff/principal, non-engineering) | 19 | No keyword evidence in title or tags |
| 10 | Expired | 20 | Duplicate of a lead already handled |

**Interview likelihood is scored, never used as a gate.** A listing is *not*
rejected just because it fails to promise "no interview" — that is rule
interpretation the spec explicitly forbids. Instead every lead is classified
`NONE / LOW / MEDIUM / HIGH` and the score prefers the low end.

---

## Opportunity score (0-100)

Answers: *"How likely is this to become a realistic paid task that this
engineer can win and complete?"* — not "how interesting is this job?"

| Component | Weight | What it measures |
| --- | --- | --- |
| Skill match | 25 | Weighted overlap with the proven stack |
| Task clarity | 15 | Concrete bounded deliverable vs vague scope |
| Payment / budget quality | 15 | Budget band **and** implied hourly value |
| Short duration | 10 | Estimated effort |
| Fixed-price / one-time | 10 | Fixed price beats hourly beats retainer |
| Freshness | 10 | Time since posting |
| Low competition | 5 | Proposals/bids already submitted |
| Client quality | 5 | Verified payment, hire rate, where published |
| Low interview likelihood | 5 | NONE/LOW preferred |

The model's own 1-10 read is applied as a **bounded ±8 adjustment**, so
deterministic evidence stays in charge of ranking. In practice the score is
mostly deterministic, which is why the agent still produces usable leads with
no model at all.

Every lead also carries: budget, estimated hours, implied hourly value, skill
match %, interview likelihood, competition, posted time, why it matches, red
flags and a recommended action.

**Worked example (from the tests):** a `$250 fixed` FastAPI bug fix scores
**86/100 HIGH PRIORITY** and outranks a `$1000/month`, long-term, 20+ hr/week,
interview-required AI Engineer role at **26/100** — a 60-point gap.

---

## Lead lifecycle

```
DISCOVERED -> QUALIFIED -> SHORTLISTED -> READY_TO_APPLY -> APPLIED
   -> REPLIED -> WON -> IN_PROGRESS -> COMPLETED -> PAID
off-path: REJECTED | IGNORED | EXPIRED
```

Every transition is appended to `lead_events`, giving an audit trail and the
raw data for the funnel KPIs. A lead that has been seen is never shown twice:
`known_lead_ids()` filters it before the model is consulted, which also makes
it impossible to re-email the same opportunity.

---

## Dashboard

`dashboard.html` — self-contained (no CDN, works offline), committed by the
scheduled workflow. The primary screen is **Today's Best Paid Tasks**:

```
94/100  HIGH PRIORITY
Fix FastAPI upload endpoint returning 500
Platform: Freelancer.com   Budget: USD 250 fixed
Duration: about 1 day      Skill match: 96%
Interview: LOW             Competition: 4 proposals
Posted: 2 hours ago        State: DISCOVERED
WHY: matches fastapi, python, postgresql; USD 250 fixed for ~8h; short scope.
ACTION: APPLY NOW
[proposal draft]
```

It also shows the pipeline (applied and beyond) and revenue/debt progress.

---

## Daily digest

Emailed to `NOTIFY_EMAIL`, plus a summary: total qualified, top opportunities,
average score, potential gross value, funnel counts and revenue.

The digest is a single daily email, not one email per listing. That is
deliberate anti-spam behaviour: the KPI is paid tasks won, not applications
sent.

---

## Proposal assistance

When a lead clears the bar, `proposals.py` drafts a short proposal that:

1. confirms understanding of the specific task,
2. cites only directly relevant experience,
3. gives a brief concrete implementation approach,
4. states a realistic delivery expectation,
5. asks at most one or two necessary questions.

The model is given a **closed fact sheet** of things Yamin has actually built
(THRYVIX AI, COGEXT, the WhatsApp/Sarvam integrations, the deployment stack)
and an explicit list of forbidden claims. Output containing a forbidden claim
is discarded and replaced with a deterministic fallback built from the same
fact sheet, so fabrication cannot reach a client. The fallback is also used
when no model is available.

---

## Anti-spam and the debt objective

Tracked, never assumed:

```
Applications/day  Qualified leads/day  Application -> reply
Reply -> paid task  Paid task -> completion  Completion -> payment
```

The KPI that matters is **PAID TASKS WON**. `--metrics` prints the funnel with
reply/win/completion/payment rates, plus revenue won / collected / pending and
progress against the debt target (default ₹4,18,000 by 2026-12-31). No income
assumption is hard-coded.

The agent does **not** auto-apply by default. `AUTO_APPLY=true` additionally
emails contacts a posting itself published, and only for scores at or above
`AUTO_APPLY_MIN_SCORE`; it stays disabled until `RESEND_FROM` is on a verified
domain, because replies sent from a shared sender never reach you.

---

## Setup

### Secrets (Settings -> Secrets and variables -> Actions -> Secrets)

| Secret | Required | Notes |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | Recommended | Primary scorer. Without it the agent runs in offline mode |
| `RESEND_API_KEY` | Yes (for email) | [resend.com](https://resend.com) -> API Keys |
| `RESEND_FROM` | No | Needed for direct outreach. Defaults to `onboarding@resend.dev` |
| `NOTIFY_EMAIL` | No | Defaults to `yaminbinyoosuf@gmail.com` owner address |
| `GEMINI_API_KEY` | No | Fallback engine |
| `COGEXT_API_KEY` | No | Commitment tracking |

### Variables (optional)

| Variable | Default | Notes |
| --- | --- | --- |
| `QUALITY_THRESHOLD` | `60` | Minimum score to be shown at all |
| `MAX_LEADS_PER_DAY` | `8` | Hard ceiling on the daily list |
| `MIN_HOURLY_VALUE` | `8` | Reject work below this implied rate |
| `MAX_JOBS_PER_RUN` | `40` | Model-screening budget per run |
| `MAX_STRONG_DRAFTS` | `6` | Reasoning-model proposals per run (biggest cost lever) |
| `REQUIRE_BUDGET` | `false` | Reject listings that hide their budget |
| `DEBT_TARGET_INR` | `418000` | Debt objective |
| `DEBT_DEADLINE` | `2026-12-31` | Debt deadline |
| `MAX_LEAD_AGE_DAYS` | `45` | Open leads older than this expire |
| `DEEPSEEK_MODEL` | `deepseek-chat` | Bulk scorer |
| `DEEPSEEK_DRAFT_MODEL` | `deepseek-v4-pro` | Reasoning model for proposals |

---

## How to run

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

export DEEPSEEK_API_KEY=sk-...
export RESEND_API_KEY=re_...

python job_agent.py                 # the daily run
python job_agent.py --dry-run       # full run, writes nothing anywhere
python job_agent.py --no-llm        # no model at all; deterministic only
python job_agent.py --self-test     # verify DeepSeek + Resend + sender
python job_agent.py --dashboard     # re-render dashboard.html
python job_agent.py --list          # print open leads
python job_agent.py --metrics       # funnel + revenue + debt
```

Record what happened — this is what makes the funnel numbers real:

```bash
python job_agent.py --mark freelancer.com:123456 APPLIED
python job_agent.py --mark freelancer.com:123456 WON
python job_agent.py --mark freelancer.com:123456 COMPLETED
python job_agent.py --revenue freelancer.com:123456 250 USD collected
```

**No API credit?** The agent degrades gracefully: hard filters and
deterministic scoring still run, and the dashboard and digest still update.
It warns and continues instead of failing the scheduled job.

---

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

144 tests, no network, no model calls, no new dependencies:

- `test_leadscore.py` — budget/date/hour parsing, interview classification,
  every hard rejection rule, score ordering, the spec's worked example
- `test_leadstore.py` — schema, idempotent upsert, full lifecycle, dedupe,
  expiry, revenue, debt progress, funnel rates, legacy import scope
- `test_pipeline.py` — end-to-end funnel over a 500-listing synthetic corpus:
  dedupe, rejection, budgeting, quality-over-quantity, persistence, rendering
- `test_dashboard.py` — digest and HTML rendering, escaping, sqlite rows
- `test_proposals.py` — grounding, forbidden-claim rejection, fallbacks

---

## Limitations

- **No official freelance APIs.** Freelancer.com is the only major marketplace
  with a usable public API. Upwork's feed is gone (HTTP 410). Algora, IssueHunt,
  Guru, PeoplePerHour and Contra were probed and do not meet the reliability
  bar, so they are not included.
- **Mercor is read from a server-rendered page**, not an official API. It can
  break if they change their front end. It fails loudly and the other ten
  sources continue.
- **Budget parsing is heuristic.** It handles symbols, ISO codes, ranges,
  hourly/monthly markers and ~30 currencies, and is defensive against malformed
  input, but a creative listing can still confuse it.
- **Estimated hours is an estimate**, from explicit statements where present and
  from budget/scope heuristics otherwise.
- **Applying is still manual.** The agent finds, scores and drafts; you place
  the bid. That is intentional — auto-bidding on your behalf risks your account.
- **`MIN_HOURLY_VALUE` is a blunt instrument.** It uses a static currency table
  for comparison, not live rates.
