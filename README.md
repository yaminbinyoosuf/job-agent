# Job Search Agent

An automated job hunter for Yamin Binyoosuf. Twice a day it:

1. Pulls six free, key-less remote job feeds — **RemoteOK**, **WeWorkRemotely**
   (programming + devops RSS), **Remotive**, **Arbeitnow**, **Jobicy**, and
   **Hacker News "Who is hiring?"**.
2. Normalizes and de-duplicates them, keeps postings inside the lookback
   window, drops obviously non-engineering roles by title, then keeps postings
   matching the keyword set.
3. Scores each posting 1–10 with **DeepSeek** and drafts a personalized
   outreach email grounded in Yamin's real resume.
4. For anything scoring 7+, emails the score, the reasoning, the apply link,
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
| `DEEPSEEK_MODEL` | `deepseek-chat` | Set to `deepseek-reasoner` for deeper scoring (slower, costlier) |
| `AUTO_APPLY` | `false` | See below |
| `MAX_JOBS_PER_RUN` | `40` | Caps API spend per run |

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

- `KEYWORDS` — positive match against title + description + tags.
- `TITLE_BLOCKLIST` — roles dropped by title alone (sales, marketing,
  accounting, recruiting, support, …). This saves DeepSeek calls on postings
  that can never be a fit.
- `SCORE_THRESHOLD` (7), `LOOKBACK_HOURS` (72), `MAX_JOBS_PER_RUN` (40) —
  overridable by environment variable.
- `PROFILE` — built from `Yamin_Bin_Yoosuf_Mercor_Final_Resume.docx`. **Update
  this when the resume changes**; it drives both scoring and the drafted emails.
- `EMAIL_TEMPLATE` — the tone/structure the drafts follow.

## `AUTO_APPLY`

Off by default, and deliberately so.

When `AUTO_APPLY=true`, the agent *additionally* sends the drafted outreach
directly to a contact address **that the posting itself published**. Postings
with no published contact still just go to your inbox.

This is the only path that emails a third party. Before turning it on, be aware
that automated cold outreach can look like spam, can breach a job board's terms,
and can burn a domain's sending reputation if it goes wrong. The agent only ever
uses addresses the posting published for contact, and ignores
`noreply@`/`privacy@`/`accessibility@`-style addresses.

## Free-tier limits

- **Job feeds**: all six are free and need no API key. RemoteOK requires a
  browser-like `User-Agent` (handled).
- **DeepSeek**: pay-as-you-go and cheap, but not free. `deepseek-chat` costs a
  fraction of a cent per posting. A capped run of 40 postings is small change;
  lower `MAX_JOBS_PER_RUN` if you want a hard ceiling.
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
