python "c:\Users\mohit\OneDrive\Desktop\FRESH START\Reports AutoMation\generate_all_lms_reports.py"

python generate_all_recon_reports.py 2026-05-26 && set REPORT_DATE=2026-05-26 && python generate_all_lms_reports.py

Reports Automation — System Context

  What This System Does

  This is an automated daily reporting system for an education CRM (LMS). It pulls data from PostgreSQL databases, builds formatted HTML reports, takes
  screenshots, and delivers them to a WhatsApp group. It also maintains a Google Sheet with daily form submissions data.

  ---
  Databases

  The system connects to 4 separate PostgreSQL databases, all accessed via asyncpg:

  ┌────────────────────────┬────────────────────────────────────────────────────┐
  │     Env Var Prefix     │                   What it holds                    │
  ├────────────────────────┼────────────────────────────────────────────────────┤
  │ REGULAR_LMS_DB_*       │ Main LMS — Regular colleges (excludes Amity, CGC)  │
  ├────────────────────────┼────────────────────────────────────────────────────┤
  │ REGULAR_CGC_LMS_DB_*   │ CGC (Chandigarh Group of Colleges) LMS             │
  ├────────────────────────┼────────────────────────────────────────────────────┤
  │ REGULAR_AMITY_LMS_DB_* │ Amity University LMS — admissions, course journeys │
  ├────────────────────────┼────────────────────────────────────────────────────┤
  │ ONLINE_LMS_DB_*        │ Online LMS — separate product line                 │
  └────────────────────────┴────────────────────────────────────────────────────┘

  Key tables used:
  - students — student leads
  - course_status_journeys — tracks every status change (form submitted, admission, etc.)
  - university_courses — maps course IDs to university names
  - counsellors — L2/L3 counsellor hierarchy
  - registrations — payment records (REGULAR DB only) — used for Amity paid forms
  - student_lead_activities — UTM/campaign data

  ---
  Files

  server.py — The Cron Scheduler

  Long-running process that orchestrates everything. Uses APScheduler to fire scripts on a UTC schedule:

  ┌───────────┬───────────┬────────────────────────────────────────┐
  │ UTC Time  │ IST Time  │               What runs                │
  ├───────────┼───────────┼────────────────────────────────────────┤
  │ 04:30 UTC │ 10:00 IST │ Recon — yesterday's full day           │
  ├───────────┼───────────┼────────────────────────────────────────┤
  │ 06:30 UTC │ 12:00 IST │ Recon — today midnight → 12 PM         │
  ├───────────┼───────────┼────────────────────────────────────────┤
  │ 10:30 UTC │ 16:00 IST │ Recon — today 12 PM → 4 PM             │
  ├───────────┼───────────┼────────────────────────────────────────┤
  │ 15:00 UTC │ 20:30 IST │ LMS Reports (Online + Regular + Amity) │
  └───────────┴───────────┴────────────────────────────────────────┘

  Also runs a smoke test on startup to verify the whole pipeline works. In Docker, injects token.json and client_secret_*.json from environment variables. 

  ---
  generate_all_lms_reports.py — Main LMS Report

  The primary report script. Runs once daily at 8:30 PM IST. Produces 3 reports in one HTML file, delivered to WhatsApp.

  Tab 1 — Online LMS Report
  - Source: ONLINE_LMS_DB
  - Shows: MTD fee collection and admissions per online supervisor/counsellor
  - Targets loaded from Google Sheet (Online_Targets, Online_Counsellors tabs)

  Tab 2 — Regular LMS Report
  - Source: REGULAR_LMS_DB, CGC_LMS_DB, AMITY_LMS_DB
  - Shows: FTD + MTD + YTD admissions and forms per college
  - Colleges tracked: Regular colleges + CGC + all Amity campuses
  - Targets loaded from Google Sheet (Regular_Targets tab)

  Tab 3 — Amity Campus YoY Report
  - Source: REGULAR_LMS_DB (paid forms via registrations table) + AMITY_LMS_DB (admissions via course_status_journeys)
  - Last year data: read from Google Sheet 1pFq4-ElGJ81y7SiaHGA6HspEpDicf93NPsPZ2k32hYU
  - Shows: MTD and YTD paid forms + admissions for each Amity campus vs last year

  Campuses tracked: Bangalore AU, Gwalior AU, Jaipur AU, Lucknow AU, Mumbai AU, Raipur AU, Gurgaon AU

  Args:
  - --local → skip WhatsApp, save HTML files locally instead
  - --nocleanup → don't delete output files after sending

  How it works:
  1. Loads config from Google Sheets
  2. Queries all DBs async
  3. Builds pandas DataFrames
  4. Renders HTML with a CSS tab system (radio input pattern)
  5. Takes screenshots per tab using Playwright
  6. Sends screenshots + HTML to WhatsApp via WHAPI
  7. Logs run to Report_Logs tab in Google Sheet

  ---
  generate_all_recon_reports.py — Recon Report

  Runs 3 times daily. Tracks API reconciliation — every payment/form event from external sources hitting the REGULAR DB. Shows counts by source/channel for
  a given time window. Delivered to WhatsApp as HTML + screenshot.

  Takes CLI args: date, optional cutoff_hour, optional start_hour to define the time window.

  ---
  dump_forms_to_sheet.py — Daily Forms Dump

  Manually triggered (or can be cron'd). Pulls all first-form submissions for a given date from all 3 LMS databases and writes them as rows to a Google    
  Sheet.

  Target sheet: 1pFq4-ElGJ81y7SiaHGA6HspEpDicf93NPsPZ2k32hYU, tab gid=3777966

  Columns written: Session, Lead Date, Lead Month, Form Date, Form Month, Lead ID, Student Name, Admission Type, Institute, Course, Fee Submitted, Team    
  Owner, Primary Lead ID, Source Name, Campaign Name

  Usage: python dump_forms_to_sheet.py 2026-06-01 (defaults to yesterday if no date given)

  The query uses course_status_journeys to find the first time a student reached any form-submitted/walkin status. Deduplicates by student+course.
  Normalizes university names to display names.

  ---
  dump_forms_to_sheet_online.py

  Same concept as dump_forms_to_sheet.py but for the Online LMS database.

  ---
  sheets_config.py — Google Sheets Config Reader

  Shared module imported by generate_all_lms_reports.py. Handles OAuth2 auth (token cached in token.json) and reads 3 config tabs:

  - Online_Targets — monthly fee + admission targets per supervisor
  - Online_Counsellors — counsellor → supervisor mapping
  - Regular_Targets — weekly/monthly targets per college with date windows
  - Report_Logs — append-only log of every report run

  Config sheet ID: 1GELJ5Win5MTonlzPqvMsC0OwQ4K_h6MJoox51WrselQ

  ---
  setup_sheets.py

  One-time helper to create the Google Sheet tabs with the correct structure if they don't exist yet.

  ---
  fake_remarks_report.py

  Generates a report flagging students whose counsellor remarks appear fake or templated (low-effort copy-paste notes). Uses the REGULAR DB.

  ---
  html_generate_all_lms_reports.py / html_generate_all_recon_reports.py

  Standalone variants that only generate the HTML file — no WhatsApp sending, no screenshots. Used for local previewing or debugging the report layout.    

  ---
  Support Files

  ┌────────────────────────────┬───────────────────────────────────────────────────────────────────────────────────────────┐
  │            File            │                                          Purpose                                          │
  ├────────────────────────────┼───────────────────────────────────────────────────────────────────────────────────────────┤
  │ .env                       │ All DB credentials + WHAPI token + group ID                                               │
  ├────────────────────────────┼───────────────────────────────────────────────────────────────────────────────────────────┤
  │ token.json                 │ Google OAuth2 access/refresh token (auto-refreshed)                                       │
  ├────────────────────────────┼───────────────────────────────────────────────────────────────────────────────────────────┤
  │ client_secret_*.json       │ Google OAuth2 client credentials                                                          │
  ├────────────────────────────┼───────────────────────────────────────────────────────────────────────────────────────────┤
  │ requirements.txt           │ Python deps: asyncpg, pandas, playwright, apscheduler, google-api-python-client, requests │
  ├────────────────────────────┼───────────────────────────────────────────────────────────────────────────────────────────┤
  │ regular_report_config.json │ Legacy config (superseded by sheets_config.py)                                            │
  ├────────────────────────────┼───────────────────────────────────────────────────────────────────────────────────────────┤
  │ report_config.json         │ Legacy config (superseded by sheets_config.py)                                            │
  └────────────────────────────┴───────────────────────────────────────────────────────────────────────────────────────────┘

  ---
  Data Flow Summary

  PostgreSQL DBs ──────┐
                       ▼
  Google Sheets ──► generate_all_lms_reports.py
  (targets +            │
   last year data)      ├─► HTML Report (3 tabs)
                        ├─► Playwright Screenshots
                        └─► WHAPI → WhatsApp Group
                                   + Report_Logs tab

  ---
  Key Google Sheets

  ┌──────────────────────────────────────────────┬─────────────────────────────────────────┐
  │                   Sheet ID                   │                 Purpose                 │
  ├──────────────────────────────────────────────┼─────────────────────────────────────────┤
  │ 1GELJ5Win5MTonlzPqvMsC0OwQ4K_h6MJoox51WrselQ │ Targets config + report logs            │
  ├──────────────────────────────────────────────┼─────────────────────────────────────────┤
  │ 1pFq4-ElGJ81y7SiaHGA6HspEpDicf93NPsPZ2k32hYU │ Daily forms dump + last year Amity data │
  └──────────────────────────────────────────────┴─────────────────────────────────────────┘