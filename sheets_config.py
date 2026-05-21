"""
Google Sheets config reader/writer for LMS Reports.
Replaces report_config.json and regular_report_config.json entirely.

Sheet ID: 1GELJ5Win5MTonlzPqvMsC0OwQ4K_h6MJoox51WrselQ

Required tabs and their column layout:

  Online_Targets  (A:C)
    A: Supervisor  B: Fee Monthly Target  C: Adm Monthly Target
    — Targets are monthly. Achievement is always compared against MTD data.

  Online_Counsellors  (A:B)
    A: Supervisor  B: Counsellor
    — All counsellor fee targets are always 0

  Regular_Targets  (A:G)
    A: College  B: Month Start  C: Month End  D: Week Start  E: Week End
    F: Adm Monthly Target  G: Forms Monthly Target
    — Period dates taken from first data row
    — Weekly/daily targets are computed by the script (monthly / weeks, monthly / days)

  Report_Logs  (A:E)  — appended by script after each run
    A: Date  B: Report  C: Grand Total Summary  D: WhatsApp Sent  E: Run At (UTC)
"""

import os
import json
from datetime import datetime

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SPREADSHEET_ID = '1GELJ5Win5MTonlzPqvMsC0OwQ4K_h6MJoox51WrselQ'

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CLIENT_SECRET = os.path.join(
    _SCRIPT_DIR,
    'client_secret_804950201435-1i6i2gtvopf98ncqdk3902lkni02g6ss.apps.googleusercontent.com.json'
)
_TOKEN_PATH = os.path.join(_SCRIPT_DIR, 'token.json')

_service = None  # module-level cache — auth once per process


def get_service():
    global _service
    if _service:
        return _service

    creds = None
    if os.path.exists(_TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(_TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(_CLIENT_SECRET, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(_TOKEN_PATH, 'w') as fh:
            fh.write(creds.to_json())

    _service = build('sheets', 'v4', credentials=creds)
    return _service


def _read_range(range_name):
    svc = get_service()
    result = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=range_name
    ).execute()
    return result.get('values', [])


def _int(val):
    try:
        return int(str(val).replace(',', '').strip())
    except (ValueError, TypeError):
        return 0


# ─── Online config ──────────────────────────────────────────────────────────────

def load_online_config():
    """
    Returns a dict that matches the old report_config.json structure:
    {
      "target_period":                 {"start_date": ..., "end_date": ...},
      "supervisor_targets":            {sup: fee_target, ...},
      "supervisor_admission_targets":  {sup: adm_target, ...},
      "counsellor_targets":            {sup: {couns: 0, ...}, ...},
    }
    """
    # --- Online_Targets (cols A:C only — no week dates needed) ---
    rows = _read_range('Online_Targets!A2:C200')
    supervisor_targets = {}
    supervisor_admission_targets = {}

    for row in rows:
        if not row or not row[0].strip():
            continue
        sup = row[0].strip()
        supervisor_targets[sup] = _int(row[1]) if len(row) > 1 else 0
        supervisor_admission_targets[sup] = _int(row[2]) if len(row) > 2 else 0

    # --- Online_Counsellors ---
    c_rows = _read_range('Online_Counsellors!A2:B500')
    counsellor_targets = {}
    for row in c_rows:
        if len(row) < 2 or not row[0].strip() or not row[1].strip():
            continue
        sup = row[0].strip()
        couns = row[1].strip()
        counsellor_targets.setdefault(sup, {})[couns] = 0

    return {
        "supervisor_targets": supervisor_targets,
        "supervisor_admission_targets": supervisor_admission_targets,
        "counsellor_targets": counsellor_targets,
    }


# ─── Regular config ─────────────────────────────────────────────────────────────

def load_regular_config():
    """
    Returns:
    {
      "target_period":  {"start_date": week_start, "end_date": week_end},
      "month_period":   {"start_date": month_start, "end_date": month_end},
      "college_targets": {
          college_name: {
              "admission_monthly": int,   # only monthly stored; script computes week/day
              "forms_monthly":     int,
          }, ...
      }
    }
    """
    rows = _read_range('Regular_Targets!A2:G200')
    college_targets = {}
    week_start = week_end = month_start = month_end = None

    for row in rows:
        if not row or not row[0].strip():
            continue
        college = row[0].strip()
        if month_start is None and len(row) >= 5:
            month_start = row[1].strip() if len(row) > 1 else ''
            month_end   = row[2].strip() if len(row) > 2 else ''
            week_start  = row[3].strip() if len(row) > 3 else ''
            week_end    = row[4].strip() if len(row) > 4 else ''
        college_targets[college] = {
            "admission_monthly": _int(row[5]) if len(row) > 5 else 0,
            "forms_monthly":     _int(row[6]) if len(row) > 6 else 0,
        }

    return {
        "target_period": {"start_date": week_start or '', "end_date": week_end or ''},
        "month_period":  {"start_date": month_start or '', "end_date": month_end or ''},
        "college_targets": college_targets,
    }


# ─── Report Logs ────────────────────────────────────────────────────────────────

def log_report(date_str, report_name, grand_total_summary, whatsapp_sent):
    """
    Appends one row to the Report_Logs tab.
    grand_total_summary: a short string e.g. "Adm: 15, Fee: ₹8.5L"
    """
    run_at = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    sent_str = 'Yes' if whatsapp_sent else 'No'
    svc = get_service()
    try:
        svc.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range='Report_Logs!A:E',
            valueInputOption='USER_ENTERED',
            body={'values': [[date_str, report_name, grand_total_summary, sent_str, run_at]]}
        ).execute()
        print(f"  📋 Logged to Report_Logs: {report_name} — {grand_total_summary}")
    except Exception as e:
        print(f"  ⚠️  Could not write to Report_Logs: {e}")
