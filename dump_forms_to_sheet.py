#!/usr/bin/env python3
"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DAILY FORMS DUMP → GOOGLE SHEET
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Queries ALL 3 regular LMS databases (REGULAR, CGC, AMITY) for yesterday's
first-form submissions and writes the combined results to the tracking sheet.

  Target Sheet: https://docs.google.com/spreadsheets/d/1pFq4-ElGJ81y7SiaHGA6HspEpDicf93NPsPZ2k32hYU
  Target Tab:   gid=3777966

Usage:  python dump_forms_to_sheet.py [YYYY-MM-DD]
        Defaults to yesterday (IST) if no date given.

Columns written (in order):
  Session | Lead Date | Lead Month | Form Date | Form Month | Lead Id |
  Student Name | Admission Type | Institute | Course | Fee Submitted |
  Team-Owner | Primary Lead ID | Source Name | Camp Name For Ref | Campaign Name
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import os
import sys

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
import json
from datetime import datetime, timedelta, UTC
from dotenv import load_dotenv

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

import asyncpg

# ─── PATHS ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.environ.get('WORKSPACE_DIR', SCRIPT_DIR)
load_dotenv(os.path.join(BASE_DIR, '.env'))

# ─── DATE ───────────────────────────────────────────────────────────────────────
if len(sys.argv) > 1:
    REPORT_DATE_STR = sys.argv[1]
else:
    now_utc = datetime.now(UTC)
    now_ist = now_utc + timedelta(hours=5, minutes=30)
    report_date = now_ist - timedelta(days=1)
    REPORT_DATE_STR = report_date.strftime('%Y-%m-%d')

print(f"📅 Report Date: {REPORT_DATE_STR} (IST midnight → next midnight)")

# ─── GOOGLE SHEETS AUTH ────────────────────────────────────────────────────────
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
TARGET_SPREADSHEET_ID = '1pFq4-ElGJ81y7SiaHGA6HspEpDicf93NPsPZ2k32hYU'
TARGET_GID = 3777966

CLIENT_SECRET = os.path.join(
    SCRIPT_DIR,
    'client_secret_804950201435-1i6i2gtvopf98ncqdk3902lkni02g6ss.apps.googleusercontent.com.json'
)
TOKEN_PATH = os.path.join(SCRIPT_DIR, 'token.json')


def get_sheets_service():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, 'w') as fh:
            fh.write(creds.to_json())
    return build('sheets', 'v4', credentials=creds)


def find_sheet_title(service):
    """Find the sheet name for the target gid."""
    meta = service.spreadsheets().get(
        spreadsheetId=TARGET_SPREADSHEET_ID,
        fields='sheets.properties(sheetId,title)'
    ).execute()
    for sheet in meta.get('sheets', []):
        props = sheet['properties']
        if props.get('sheetId') == TARGET_GID:
            return props['title']
    raise RuntimeError(f"No sheet found with gid={TARGET_GID} in spreadsheet {TARGET_SPREADSHEET_ID}")


# ─── DB CONFIGS ─────────────────────────────────────────────────────────────────
DB_CONFIGS = [
    {
        "name": "REGULAR",
        "host": os.getenv("REGULAR_LMS_DB_HOST"),
        "port": int(os.getenv("REGULAR_LMS_DB_PORT", "54321")),
        "database": os.getenv("REGULAR_LMS_DB_NAME"),
        "user": os.getenv("REGULAR_LMS_DB_USER"),
        "password": os.getenv("REGULAR_LMS_DB_PASSWORD"),
    },
    {
        "name": "CGC",
        "host": os.getenv("REGULAR_CGC_LMS_DB_HOST"),
        "port": int(os.getenv("REGULAR_CGC_LMS_DB_PORT", "54321")),
        "database": os.getenv("REGULAR_CGC_LMS_DB_NAME"),
        "user": os.getenv("REGULAR_CGC_LMS_DB_USER"),
        "password": os.getenv("REGULAR_CGC_LMS_DB_PASSWORD"),
    },
    {
        "name": "AMITY",
        "host": os.getenv("REGULAR_AMITY_LMS_DB_HOST"),
        "port": int(os.getenv("REGULAR_AMITY_LMS_DB_PORT", "54321")),
        "database": os.getenv("REGULAR_AMITY_LMS_DB_NAME"),
        "user": os.getenv("REGULAR_AMITY_LMS_DB_USER"),
        "password": os.getenv("REGULAR_AMITY_LMS_DB_PASSWORD"),
    },
]

# ─── QUERY (param: date_str as YYYY-MM-DD) ─────────────────────────────────────
# Exclusion clause is injected per DB to avoid cross-DB duplicates:
#   REGULAR DB excludes Amity/CGC/Landran colleges
#   CGC and AMITY DBs serve their own colleges exclusively
FORMS_QUERY = """
WITH first_form AS (
    SELECT
        student_id,
        course_id,
        assigned_l3_counsellor_id,
        MIN(created_at) AS first_form_date
    FROM course_status_journeys
    WHERE course_status IN (
        'Form Submitted – Portal Pending',
        'Form Submitted – Completed',
        'Walkin Completed',
        'Exam/Interview Scheduled',
        'Offer Letter/Results Pending',
        'Offer Letter/Results Released',
        'Ready For Admission'
    )
    AND student_id IN (SELECT student_id FROM students)
    GROUP BY student_id, course_id, assigned_l3_counsellor_id
),
latest_fee_type AS (
    SELECT DISTINCT ON (student_id, course_id)
        student_id, course_id, fee_type AS admission_type
    FROM course_status_journeys
    WHERE student_id IN (SELECT student_id FROM students)
    ORDER BY student_id, course_id, created_at DESC
),
deduped_deposit AS (
    SELECT DISTINCT ON (student_id, course_id, fee_type, created_at::DATE)
        student_id, course_id, deposit_amount
    FROM course_status_journeys
    WHERE deposit_amount > 0
      AND student_id IN (SELECT student_id FROM students)
    ORDER BY student_id, course_id, fee_type, created_at::DATE, created_at DESC
),
total_deposit AS (
    SELECT student_id, course_id, SUM(deposit_amount) AS total_deposit
    FROM deduped_deposit
    GROUP BY student_id, course_id
),
deduped_sla AS (
    SELECT DISTINCT ON (student_id)
        student_id, utm_campaign
    FROM student_lead_activities
    ORDER BY student_id, created_at ASC
)
SELECT
    CASE
        WHEN EXTRACT(MONTH FROM ff.first_form_date AT TIME ZONE 'Asia/Kolkata') >= 4
        THEN TO_CHAR(EXTRACT(YEAR FROM ff.first_form_date AT TIME ZONE 'Asia/Kolkata')::int, 'FM0000')
             || '-'
             || TO_CHAR((EXTRACT(YEAR FROM ff.first_form_date AT TIME ZONE 'Asia/Kolkata')::int + 1) % 100, 'FM00')
        ELSE TO_CHAR((EXTRACT(YEAR FROM ff.first_form_date AT TIME ZONE 'Asia/Kolkata')::int - 1), 'FM0000')
             || '-'
             || TO_CHAR(EXTRACT(YEAR FROM ff.first_form_date AT TIME ZONE 'Asia/Kolkata')::int % 100, 'FM00')
    END AS session,
    (s.created_at AT TIME ZONE 'Asia/Kolkata')::date AS lead_date,
    TO_CHAR(s.created_at AT TIME ZONE 'Asia/Kolkata', 'Mon-YYYY') AS lead_month,
    (ff.first_form_date AT TIME ZONE 'Asia/Kolkata')::date AS form_date,
    TO_CHAR(ff.first_form_date AT TIME ZONE 'Asia/Kolkata', 'Mon-YYYY') AS form_month,
    s.student_id AS lead_id,
    s.student_name,
    lft.admission_type,
    uc.university_name AS institute,
    uc.course_name AS course,
    COALESCE(td.total_deposit, 0) AS course_fee_submitted,
    l2_c.counsellor_name AS team_owner,
    s.student_id AS primary_lead_id,
    s.source AS source_name,
    COALESCE(sla.utm_campaign) AS camp_name_for_ref,
    COALESCE(sla.utm_campaign) AS campaign_name
FROM first_form ff
JOIN students s ON ff.student_id = s.student_id
JOIN university_courses uc ON ff.course_id = uc.course_id
LEFT JOIN counsellors l2_c ON s.assigned_counsellor_id = l2_c.counsellor_id
    AND l2_c.role = 'l2'
LEFT JOIN latest_fee_type lft ON ff.student_id = lft.student_id AND ff.course_id = lft.course_id
LEFT JOIN total_deposit td ON ff.student_id = td.student_id AND ff.course_id = td.course_id
LEFT JOIN deduped_sla sla ON ff.student_id = sla.student_id
WHERE ff.first_form_date >= $1::date - interval '5 hours 30 minutes'
  AND ff.first_form_date <  $1::date + interval '1 day' - interval '5 hours 30 minutes'
  {exclude_clause}
ORDER BY ff.first_form_date DESC
"""

# Exclude clause for REGULAR DB to avoid double-counting Amity/CGC colleges
_REGULAR_EXCLUDE = """
  AND uc.university_name NOT ILIKE '%Amity%'
  AND uc.university_name NOT ILIKE '%Chandigarh Group%'
  AND uc.university_name NOT ILIKE '%CGC%'
  AND uc.university_name NOT ILIKE '%Landran%'"""


# ─── COLLEGE NAME NORMALIZATION ───────────────────────────────────────────────

def normalize_institute(name: str) -> str:
    """Map raw DB university names to standardized display names."""
    n = (name or '').lower()
    if 'lovely' in n:
        return 'Lovely Professional University , Phagwara'
    if 'chandigarh university' in n and 'lucknow' in n:
        return 'Chandigarh University, Lucknow'
    if 'chandigarh university' in n:
        return 'Chandigarh University, Mohali'
    if 'cgc' in n or 'chandigarh group' in n or 'landran' in n:
        return 'Chandigarh Group Of Colleges - CGC Landran'
    if 'amity' in n and 'lucknow' in n:
        return 'Amity University , Lucknow'
    if 'amity' in n and 'jaipur' in n:
        return 'Amity University , Jaipur'
    if 'amity' in n and 'mumbai' in n:
        return 'Amity University , Mumbai'
    if 'amity' in n and 'raipur' in n:
        return 'Amity University , Raipur'
    if 'amity' in n and 'gwalior' in n:
        return 'Amity University , Gwalior'
    if 'amity' in n and 'bangalore' in n:
        return 'Amity University , Bangalore'
    if 'amity' in n and 'gurugram' in n:
        return 'Amity University , gurugram'
    return name  # fallback: keep original


# ─── FETCH FROM ALL DBS ────────────────────────────────────────────────────────

async def fetch_all():
    all_rows = []
    for db in DB_CONFIGS:
        excl = _REGULAR_EXCLUDE if db['name'] == 'REGULAR' else ''
        conn = await asyncpg.connect(
            host=db['host'], port=db['port'], database=db['database'],
            user=db['user'], password=db['password']
        )
        try:
            sql = FORMS_QUERY.format(exclude_clause=excl)
            rows = await conn.fetch(sql, datetime.strptime(REPORT_DATE_STR, '%Y-%m-%d').date())
            count = len(rows)
            print(f"  [{db['name']}] {count} rows")
            for r in rows:
                d = dict(r)
                all_rows.append([
                    d['session'],
                    str(d['lead_date']) if d['lead_date'] else '',
                    d['lead_month'],
                    str(d['form_date']) if d['form_date'] else '',
                    d['form_month'],
                    str(d['lead_id']),
                    d['student_name'],
                    d['admission_type'] or '',
                    normalize_institute(d['institute']),
                    d['course'],
                    str(d['course_fee_submitted']) if d['course_fee_submitted'] else '0',
                    d['team_owner'] or '',
                    str(d['primary_lead_id']),
                    d['source_name'] or '',
                    d['camp_name_for_ref'] or '',
                    d['campaign_name'] or '',
                ])
        finally:
            await conn.close()
    return all_rows


# ─── WRITE TO GOOGLE SHEET ─────────────────────────────────────────────────────

def write_to_sheet(rows, sheet_title):
    service = get_sheets_service()

    headers = [
        'Session', 'Lead Date', 'Lead Month', 'Form Date', 'Form Month', 'Lead Id',
        'Student Name', 'Admission Type', 'Institute', 'Course', 'Fee Submitted',
        'Team-Owner', 'Primary Lead ID', 'Source Name', 'Camp Name For Ref', 'Campaign Name'
    ]

    # Clear everything from A1 down
    print(f"  Clearing existing data from '{sheet_title}'...")
    service.spreadsheets().values().clear(
        spreadsheetId=TARGET_SPREADSHEET_ID,
        range=f"'{sheet_title}'!A1:P"
    ).execute()

    # Write headers + data in one batch
    all_data = [headers] + rows
    service.spreadsheets().values().update(
        spreadsheetId=TARGET_SPREADSHEET_ID,
        range=f"'{sheet_title}'!A1",
        valueInputOption='USER_ENTERED',
        body={'values': all_data}
    ).execute()

    print(f"  ✅ Wrote {len(rows)} rows to '{sheet_title}'")


# ─── MAIN ──────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("📊 DAILY FORMS DUMP → GOOGLE SHEET")
    print("=" * 60)
    print(f"   Date: {REPORT_DATE_STR}")
    print()

    print("─── Fetching from databases ───────────────────────────────────────")
    rows = await fetch_all()
    total = len(rows)
    print(f"   Total rows across all DBs: {total}")
    print()

    print("─── Writing to Google Sheet ───────────────────────────────────────")
    sheet_title = find_sheet_title(get_sheets_service())
    print(f"   Sheet: '{sheet_title}' (gid={TARGET_GID})")
    write_to_sheet(rows, sheet_title)

    print()
    print("=" * 60)
    print(f"✅ DUMP COMPLETE — {total} records written")
    print("=" * 60)


if __name__ == '__main__':
    asyncio.run(main())
