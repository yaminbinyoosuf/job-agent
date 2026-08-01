# Job Search Agent

Automated job hunter for Yamin. Twice a day it:

1. Pulls the live [RemoteOK](https://remoteok.com/api) feed (free, no key needed).
2. Filters to postings from the last 24 hours matching: `AI agent`, `FastAPI`,
   `WhatsApp`, `voice agent`, `LLM`, `Python`.
3. Sends each match to Gemini (`gemini-1.5-flash`) to score fit 1-10 and draft
   a personalized outreach email based on the profile/template baked into
   `job_agent.py`.
4. For anything scoring 7+, emails the draft + job link via
   [Resend](https://resend.com) so it can be reviewed and pasted into the
   real application form within minutes of the posting going live.
5. Logs every job it looked at (matched or not, emailed or not) to
   `jobs_log.csv`, and skips job IDs it's already logged on future runs.

**Important — this does not cold-email companies directly.** RemoteOK listings
link to an apply page, not a hiring manager's inbox, so there's no address to
send to. The agent instead emails the drafted outreach *to you*
(`NOTIFY_EMAIL`, default `yaminbinyoosuf@gmail.com`) so you can send it
through the actual application channel.

## Free-tier limits to know about

- **RemoteOK**: free, unauthenticated, no rate-limit key needed. It does
  block requests with no `User-Agent` header — already handled in the script.
- **Gemini API**: `gemini-1.5-flash` has a genuinely free tier (generous daily
  request quota at the time of writing) — this script's usage (a handful of
  short scoring calls per run, twice a day) should comfortably stay within it.
- **Resend free tier**: 100 emails/day, 3,000/month, no credit card. The
  catch: until you verify a domain you own in Resend, you can only send
  **from** `onboarding@resend.dev` and only **to the email address on your
  Resend account**. That's exactly this script's default setup (draft goes to
  your own inbox), so no domain verification is required to get started.
- **GitHub Actions**: free for public repos; 2,000 min/month free on private
  repos, which this workflow uses only a few minutes of per month.

## Setup

### 1. Get your API keys

- **Gemini API key**: [aistudio.google.com/apikey](https://aistudio.google.com/apikey) → Create API key (free).
- **Resend API key**: [resend.com](https://resend.com) → sign up (free) → API Keys.
  Use the key tied to the account whose inbox you want drafts sent to.

### 2. Push this folder to a GitHub repo

```bash
cd job-agent
git init
git add .
git commit -m "Add job search agent"
git branch -M main
git remote add origin https://github.com/<you>/job-agent.git
git push -u origin main
```

### 3. Add repo secrets

GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret name         | Required | Value |
|----------------------|----------|-------|
| `GEMINI_API_KEY`     | Yes | Your Gemini key |
| `RESEND_API_KEY`     | Yes | Your Resend key |
| `RESEND_FROM`        | No  | Defaults to `onboarding@resend.dev`. Only set this once you've verified your own domain in Resend. |
| `NOTIFY_EMAIL`       | No  | Defaults to `yaminbinyoosuf@gmail.com`. Must match the email on your Resend account until you verify a domain. |

### 4. Enable the workflow

The workflow at `.github/workflows/job_agent.yml` runs automatically at
8:00 AM and 6:00 PM IST (`30 2 * * *` and `30 12 * * *` UTC) once it's on
`main`. It also needs **write access to push the updated `jobs_log.csv`** —
GitHub Actions' default token has this by default for repos you own; if pushes
fail, check Settings → Actions → General → Workflow permissions is set to
"Read and write permissions".

To test immediately instead of waiting for the schedule: GitHub repo →
**Actions → Job Search Agent → Run workflow**.

### 5. Run it locally (optional, for testing)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

export GEMINI_API_KEY=AIza...
export RESEND_API_KEY=re_...
# optional:
# export RESEND_FROM=you@yourdomain.com
# export NOTIFY_EMAIL=you@example.com

python job_agent.py
```

## Files

- `job_agent.py` — the agent
- `requirements.txt` — Python deps
- `.github/workflows/job_agent.yml` — cron schedule
- `jobs_log.csv` — created automatically on first run; tracks every job seen
  so re-runs don't re-score or re-email the same posting

## Tuning

- **Keywords / score threshold / lookback window**: edit the constants at the
  top of `job_agent.py` (`KEYWORDS`, `SCORE_THRESHOLD`, `LOOKBACK_HOURS`).
- **Profile / email template**: edit `PROFILE` and `EMAIL_TEMPLATE` in
  `job_agent.py` — Gemini uses these directly to draft outreach.
- **Model**: `GEMINI_MODEL` is set to `gemini-1.5-flash`. Swap it for
  `gemini-1.5-pro` (or a newer Gemini model) if you want stronger scoring at
  higher per-call cost/lower free-tier quota.
- **Other job boards**: RemoteOK is the only source for now, per the "start
  simple, free tools only" brief. Adding a second free source (e.g. a
  We Work Remotely RSS feed) is a matter of writing another `fetch_*` function
  and merging its output into `filter_recent_matching_jobs`.
