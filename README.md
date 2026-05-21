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
