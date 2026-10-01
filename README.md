# Job Search Agent

An automated job hunter for Yamin Binyoosuf. Twice a day it:

1. Pulls eight free, key-less remote job feeds — **RemoteOK**, **WeWorkRemotely**
   (programming + devops RSS), **Remotive**, **Arbeitnow**, **Jobicy**,
   **Himalayas**, **Working Nomads**, and **Hacker News "Who is hiring?"**.
2. Normalizes and de-duplicates them, keeps postings inside the lookback
   window, drops non-engineering and out-of-reach seniority by title, then
   requires keyword evidence in the title/tags (not just buried in the body).
3. Scores each posting 1–10 with **DeepSeek** and drafts a personalized
   outreach email grounded in Yamin's real resume.
4. For anything scoring 6+, emails the score, the reasoning, the apply link,
   any hiring contact published in the posting, and a ready-to-send outreach
   draft to `NOTIFY_EMAIL`.
5. Logs every posting it looked at to `jobs_log.csv`, and skips job IDs it has
   already finished with on later runs.

**This does not cold-email companies.** Listings link to an apply page, not a
hiring manager's inbox. The agent emails the drafted outreach *to you* so you
can send it through the real application channel within minutes of a posting
going live. See `AUTO_APPLY` below for the one exception.

## Setup

### 1. Repo secrets

GitHub repo → **Settings → Secrets and variables → Actions → Secrets**:

| Secret | Required | Notes |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | **Yes** | Primary engine. [platform.deepseek.com](https://platform.deepseek.com/api_keys) |
| `RESEND_API_KEY` | **Yes** | Email delivery. [resend.com](https://resend.com) → API Keys |
| `RESEND_FROM` | No | Defaults to `onboarding@resend.dev` |
| `NOTIFY_EMAIL` | No | Defaults to `yaminbinyoosuf@gmail.com`. Must match your Resend account email until you verify a domain. |
| `GEMINI_API_KEY` | No | Fallback engine, used only if `DEEPSEEK_API_KEY` is missing |
| `COGEXT_API_KEY` | No | Commitment tracking |

### 2. Repo variables

GitHub repo → **Settings → Secrets and variables → Actions → Variables**
(all optional):

| Variable | Default | Notes |
| --- | --- | --- |
| `DEEPSEEK_MODEL` | `deepseek-chat` | Fast, cheap model that scores every posting |
| `DEEPSEEK_DRAFT_MODEL` | `deepseek-v4-pro` | Stronger reasoning model that rewrites the outreach **only for scoring matches**. Set to empty to disable the second pass |
| `AUTO_APPLY` | `false` | See below |
| `AUTO_APPLY_MIN_SCORE` | `8` | Score a posting needs before direct outreach is sent |
| `MAX_JOBS_PER_RUN` | `40` | Caps API spend per run |

### Why two models

Measured on a real posting against this account:

| Model | Time/job | Output tokens | Kind |
| --- | --- | --- | --- |
| `deepseek-chat` | 1.9s | 342 | standard |
| `deepseek-flash` | 11s | 2,243 | reasoning |
| `deepseek-v4-pro` | 31s | 3,264 | reasoning |

Scoring every posting with a reasoning model costs ~10x the tokens and ~16x
the time for no measurable gain in ranking. So `deepseek-chat` scores the full
queue, and `deepseek-v4-pro` is spent only on the handful of postings that
already cleared the threshold — where the quality of the actual email matters.
`DEEPSEEK_MAX_TOKENS` defaults to 8000 because reasoning models spend the
budget on `reasoning_content` *before* emitting an answer; too low a cap makes
them return empty content.

### 3. Verify the wiring

GitHub repo → **Actions → Job Search Agent → Run workflow** → choose
**`self-test`**. This makes one real DeepSeek call and sends one test email, and
tells you exactly which piece is broken — no guessing.

Then run **`dry-run`**: it exercises the whole pipeline, scores real postings,
and writes the log, but sends no email.

The scheduled runs (`08:00` and `18:00` IST) start automatically once the
workflow is on `master`.

## Run locally

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

export DEEPSEEK_API_KEY=sk-...
export RESEND_API_KEY=re_...

python job_agent.py --self-test   # check the wiring
python job_agent.py --dry-run     # full pipeline, no email sent
python job_agent.py               # the real thing
```

## Tuning

All the knobs are near the top of `job_agent.py`:

- `KEYWORDS` — positive signal. A keyword in the title or board tags counts
  double; body-only mentions need three to qualify, because a marketing post
  that says "automation" is not an engineering job.
- `TITLE_BLOCKLIST` / `GERMAN_TITLE_RE` — roles dropped by title alone (sales,
  marketing, accounting, recruiting, support, designer, and
  principal/staff/director/architect level). This saves DeepSeek calls on
  postings that can never be a fit.
- `SCORE_THRESHOLD` (6), `LOOKBACK_HOURS` (72), `MAX_JOBS_PER_RUN` (40) —
  overridable by environment variable. 6 deliberately surfaces "worth a shot
  with a real gap" roles, not just perfect matches.
- `PROFILE` — built from `Yamin_Bin_Yoosuf_Mercor_Final_Resume.docx`. **Update
  this when the resume changes**; it drives both scoring and the drafted emails.
- `EMAIL_TEMPLATE` — the tone/structure the drafts follow.

## `AUTO_APPLY`

Off by default, and gated on a verified sender.

When `AUTO_APPLY=true`, the agent *additionally* sends the drafted outreach
directly to a contact address **that the posting itself published**, but only
when the posting scores at least `AUTO_APPLY_MIN_SCORE` (default 8). Postings
with no published contact, or below that score, still just go to your inbox.

**This only works once you verify a domain.** Resend's shared
`onboarding@resend.dev` sender can only deliver to your own account address.
Even where it is accepted, a reply from a hiring manager would land at Resend
instead of at you — which defeats the entire point. So the agent refuses to
send direct outreach while `RESEND_FROM` is still a `resend.dev` address and
tells you so at startup, rather than silently failing or sending from a
spam-looking address.

To turn it on properly:

1. [resend.com/domains](https://resend.com/domains) → **Add Domain** →
   `thryvixai.com` (or `cogextai.com`) → add the DNS records it shows.
2. Set the `RESEND_FROM` secret to an address on that domain, e.g.
   `yamin@thryvixai.com`.
3. Run the **`self-test`** workflow — it prints
   `Direct outreach: READY` when the wiring is good.

Replies always go to `OUTREACH_REPLY_TO` (defaults to `NOTIFY_EMAIL`), so
hiring managers reach you personally even when the mail is sent by the agent.

Before enabling it, be aware that automated cold outreach can look like spam,
can breach a job board's terms, and can hurt a domain's sending reputation.
The agent only ever uses addresses a posting published for contact, and
ignores `noreply@`, `privacy@`, `legal@`, `support@` and `accessibility@`
style addresses.

## Free-tier limits

- **Job feeds**: all six are free and need no API key. RemoteOK requires a
  browser-like `User-Agent` (handled).
- **DeepSeek**: pay-as-you-go and cheap, but not free. `deepseek-chat` costs a
  fraction of a cent per posting. A capped run of 40 postings is small change;
  lower `MAX_JOBS_PER_RUN` if you want a hard ceiling.
- **Himalayas / Working Nomads / Jobicy / Remotive / Arbeitnow / WWR**: free, unauthenticated.
- **Resend**: 100 emails/day, 3,000/month free. Until you verify your own
  domain you can only send **from** `onboarding@resend.dev` and only **to** the
  address on your Resend account — which is exactly how this is configured.
- **GitHub Actions**: a few minutes per month.

## Files

- `job_agent.py` — the agent
- `requirements.txt` — Python deps
- `.github/workflows/job_agent.yml` — schedule + manual dispatch
- `jobs_log.csv` — every posting seen, so re-runs don't re-score or re-email

## Troubleshooting

Every scheduled run failed from 2026-09-06 to 2026-10-01. The causes, all fixed:

1. **`NameError: name 'genai' is not defined`** — commit `a661b8e` renamed the
   Gemini import to `_genai` but left the annotation `client: genai.Client`.
   Python evaluates annotations at import time, so the script died before
   `main()` ran. Fixed with `from __future__ import annotations`.
2. **`DEEPSEEK_API_KEY` was never passed to the workflow**, so DeepSeek — the
   documented default engine — was never used and the code fell through to a
   Gemini path whose package wasn't installed.
3. **Log schema drift** — `jobs_log.csv` used `source`/`notified` while the
   code wrote `emailed`. `DictReader` returned no `emailed` value, so every
   strong match looked un-emailed and was re-scored and re-emailed on every
   run. `migrate_log_if_needed()` now repairs the header on startup (keeping a
   `.bak`), and `requirements.txt` pins the fallback SDK.

If a run fails, open the failed step's log — the agent prints the failing
source, the HTTP status, and the model error rather than swallowing them.
