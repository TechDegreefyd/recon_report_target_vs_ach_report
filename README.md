# Reports Automation

Automated daily report generation for Online and Regular LMS data, plus API Recon reports. Reports are generated as HTML dashboards and delivered to a WhatsApp Admin group via WHAPI.

---

## Scripts

| Script | What it does |
|---|---|
| `generate_all_lms_reports.py` | Generates Online LMS + Regular LMS HTML dashboards |
| `generate_all_recon_reports.py` | Generates API Recon reports (All Sources + Branded Campaigns) |
| `sheets_config.py` | Reads targets/config from Google Sheets, logs runs to `Report_Logs` tab |
| `setup_sheets.py` | One-time Google Sheets OAuth setup |

---

## Output

| Report | Output Path |
|---|---|
| Online LMS HTML | `Automation Cron Job/Target Report/Degreefyd_Online_LMS_HTML_Report_<timestamp>.html` |
| Regular LMS HTML | `Automation Cron Job/Target Report/Degreefyd_Regular_LMS_HTML_Report_<timestamp>.html` |
| Recon All Sources | `Automation Cron Job/Recon Data/Regular_Recon_All_<timestamp>.html` |
| Recon Branded | `Automation Cron Job/Recon Data/Regular_Recon_Branded_<timestamp>.html` |

If WhatsApp delivery fails, LMS reports are also copied to `Automation Cron Job/Target Report/local_fallback/`.

---

## Setup

### 1. Install dependencies

```bash
pip install asyncpg pandas requests python-dotenv google-auth google-auth-oauthlib google-api-python-client
```

### 2. Configure environment

```bash
cp .env.example .env
```

Fill in `.env` with your database credentials and WHAPI token. There are 4 databases:
- `ONLINE_LMS` — Online admissions DB
- `REGULAR_LMS` — Main regular admissions DB
- `REGULAR_CGC_LMS` — CGC-specific DB
- `REGULAR_AMITY_LMS` — Amity-specific DB

### 3. Google Sheets auth

Place your `client_secret_*.json` file in the project root, then run:

```bash
python setup_sheets.py
```

This opens a browser for OAuth consent and saves `token.json` for future runs.

### Google Sheet structure

Sheet ID: `1GELJ5Win5MTonlzPqvMsC0OwQ4K_h6MJoox51WrselQ`

| Tab | Columns | Purpose |
|---|---|---|
| `Online_Targets` | A: Supervisor, B: Fee Target, C: Adm Target | Monthly targets per supervisor |
| `Online_Counsellors` | A: Supervisor, B: Counsellor | Counsellor roster |
| `Regular_Targets` | A: College, B-E: Date ranges, F: Adm Target, G: Forms Target | College targets + week/month dates |
| `Report_Logs` | A-E | Auto-appended after each run |

---

## Usage

### Run for today (auto-detects date in IST, rolls back to previous day before 6 AM)

```bash
python generate_all_lms_reports.py
python generate_all_recon_reports.py
```

### Run for a specific date

```bash
# LMS reports — set env var
REPORT_DATE=2026-05-20 python generate_all_lms_reports.py

# Recon reports — pass as argument
python generate_all_recon_reports.py 2026-05-20
```

### Windows (PowerShell)

```powershell
$env:REPORT_DATE = "2026-05-20"; python generate_all_lms_reports.py
python generate_all_recon_reports.py 2026-05-20
```

---

## What each report shows

### Online LMS
- **Overview tab** — Team owner snapshot cards + summary table (fee & admissions vs target)
- **Fee Collected tab** — Counsellor-wise fee breakdown (MTD vs monthly target)
- **Admissions tab** — Counsellor-wise admission counts
- **Colleges tab** — College-wise Forms → Admissions conversion (YTD / MTD / FTD)

### Regular LMS
- **Admissions tab** — College-wise admissions vs target (YTD / Month / Week / FTD)
- **Forms tab** — College-wise forms submitted vs target (YTD / Month / Week / FTD)

### API Recon
- **All Sources** — Every API recon entry for the day grouped by college → Auto/Manual → Proceed/Fail/DNP
- **Branded Campaigns** — Same view filtered to students matching branded UTM campaign patterns or campaign IDs

---

## Environment Variables

| Variable | Description |
|---|---|
| `WORKSPACE_DIR` | Override base directory (defaults to script directory) |
| `REPORT_DATE` | Override report date as `YYYY-MM-DD` (LMS script only) |
| `WHAPI_TOKEN` | WHAPI bearer token for WhatsApp delivery |
| `WHATSAPP_GROUP` | WhatsApp group ID (default: `120363426619711887@g.us`) |
| `ONLINE_LMS_DB_*` | Online LMS PostgreSQL connection details |
| `REGULAR_LMS_DB_*` | Regular LMS PostgreSQL connection details |
| `REGULAR_CGC_LMS_DB_*` | CGC PostgreSQL connection details |
| `REGULAR_AMITY_LMS_DB_*` | Amity PostgreSQL connection details |
| `GOOGLE_TOKEN_JSON` | Full contents of `token.json` (used by Dokploy/Docker deployments) |
| `GOOGLE_CLIENT_SECRET_JSON` | Full contents of `client_secret_*.json` (used by Dokploy/Docker deployments) |

---

## Deploying on a VPS with Dokploy

These scripts are packaged as a Docker image and run as a **Cron Job** in Dokploy. The container starts, runs both scripts, and exits — no long-running process needed.

### Files included

| File | Purpose |
|---|---|
| `Dockerfile` | Builds the Python image with all dependencies |
| `entrypoint.sh` | Injects secrets from env vars, then runs the scripts |
| `requirements.txt` | Python dependency list |
| `.dockerignore` | Keeps `.env`, `token.json`, and output files out of the image |

---

### Step 1 — Push deployment files to Git

```powershell
git add requirements.txt Dockerfile entrypoint.sh .dockerignore
git commit -m "Add Dokploy deployment config"
git push
```

---

### Step 2 — Create a Cron Job in Dokploy

1. Dokploy → **New** → **Cron Job**
2. **Source**: connect your Git repo
3. **Build type**: Dockerfile
4. **Schedule**: `30 3 * * *` (= 9:00 AM IST daily)
5. **Command**: `/entrypoint.sh both`
   - Use `/entrypoint.sh lms` to run only LMS reports
   - Use `/entrypoint.sh recon` to run only Recon reports

---

### Step 3 — Set Environment Variables in Dokploy

Add all of the following under the cron job's **Environment** tab:

```
# Google credentials (paste single-line minified JSON from Step 1)
GOOGLE_TOKEN_JSON={"token":"...","refresh_token":"...","..."}
GOOGLE_CLIENT_SECRET_JSON={"installed":{"client_id":"...","..."}}}

# WhatsApp
WHAPI_TOKEN=your_whapi_token
WHATSAPP_GROUP=120363426619711887@g.us

# Online LMS DB
ONLINE_LMS_DB_HOST=
ONLINE_LMS_DB_PORT=54321
ONLINE_LMS_DB_NAME=
ONLINE_LMS_DB_USER=
ONLINE_LMS_DB_PASSWORD=

# Regular LMS DB
REGULAR_LMS_DB_HOST=
REGULAR_LMS_DB_PORT=54321
REGULAR_LMS_DB_NAME=
REGULAR_LMS_DB_USER=
REGULAR_LMS_DB_PASSWORD=

# CGC DB
REGULAR_CGC_LMS_DB_HOST=
REGULAR_CGC_LMS_DB_PORT=54321
REGULAR_CGC_LMS_DB_NAME=
REGULAR_CGC_LMS_DB_USER=
REGULAR_CGC_LMS_DB_PASSWORD=

# Amity DB
REGULAR_AMITY_LMS_DB_HOST=
REGULAR_AMITY_LMS_DB_PORT=54321
REGULAR_AMITY_LMS_DB_NAME=
REGULAR_AMITY_LMS_DB_USER=
REGULAR_AMITY_LMS_DB_PASSWORD=
```

> Secrets are never baked into the Docker image — `.env`, `token.json`, and `client_secret_*.json` are all gitignored and dockerignored. Everything comes in through Dokploy env vars at runtime.

---

### How Google auth works on the VPS (no browser needed)

When the container starts, `entrypoint.sh` writes `token.json` from the `GOOGLE_TOKEN_JSON` env var. The Google client library loads it, sees a valid refresh token, and silently fetches a new access token — no browser, no URL, no manual step.

The browser login (`run_local_server`) only triggers if `token.json` is missing or has no refresh token. Since you're injecting it from your already-authenticated local file, it never triggers.

The refresh token does not expire unless you explicitly revoke it in your Google account. Each cron run starts fresh from the same refresh token, so there is no state to persist between runs.
