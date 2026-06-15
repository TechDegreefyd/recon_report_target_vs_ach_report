"""
One-time (or re-run) setup script.
Creates all 4 required tabs in the Google Sheet and populates them.
No JSON files needed - this IS the source of truth.

Run:  python setup_sheets.py
"""

import os
import sys
import calendar
from datetime import datetime, timedelta

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# ── Dynamic date helpers (recomputed every time setup_sheets.py is run) ────────
_today       = datetime.today()
_month_start = _today.replace(day=1).strftime('%Y-%m-%d')
_month_end   = _today.replace(day=calendar.monthrange(_today.year, _today.month)[1]).strftime('%Y-%m-%d')
_monday      = (_today - timedelta(days=_today.weekday())).strftime('%Y-%m-%d')
_sunday      = (_today + timedelta(days=6 - _today.weekday())).strftime('%Y-%m-%d')

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

SCOPES         = ['https://www.googleapis.com/auth/spreadsheets']
SPREADSHEET_ID = '1GELJ5Win5MTonlzPqvMsC0OwQ4K_h6MJoox51WrselQ'

_DIR           = os.path.dirname(os.path.abspath(__file__))
_CLIENT_SECRET = os.path.join(
    _DIR,
    'client_secret_804950201435-1i6i2gtvopf98ncqdk3902lkni02g6ss.apps.googleusercontent.com.json'
)
_TOKEN_PATH = os.path.join(_DIR, 'token.json')


# ---- Auth ----------------------------------------------------------------------
def get_service():
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
    return build('sheets', 'v4', credentials=creds)


# ---- Sheet helpers -------------------------------------------------------------
def get_existing_tabs(svc):
    meta = svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    return {s['properties']['title']: s['properties']['sheetId']
            for s in meta.get('sheets', [])}


def ensure_tab(svc, title, existing):
    if title in existing:
        print(f"  [exists]  {title}")
        return existing[title]
    resp = svc.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={'requests': [{'addSheet': {'properties': {'title': title}}}]}
    ).execute()
    sheet_id = resp['replies'][0]['addSheet']['properties']['sheetId']
    print(f"  [created] {title}")
    return sheet_id


def write_tab(svc, tab, rows):
    svc.spreadsheets().values().clear(
        spreadsheetId=SPREADSHEET_ID, range=f'{tab}!A1'
    ).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f'{tab}!A1',
        valueInputOption='USER_ENTERED',
        body={'values': rows}
    ).execute()


def format_tab(svc, sheet_id, ncols):
    svc.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={'requests': [
            {'repeatCell': {
                'range': {'sheetId': sheet_id, 'startRowIndex': 0, 'endRowIndex': 1,
                          'startColumnIndex': 0, 'endColumnIndex': ncols},
                'cell': {'userEnteredFormat': {
                    'textFormat': {'bold': True},
                    'backgroundColor': {'red': 0.85, 'green': 0.92, 'blue': 0.98}
                }},
                'fields': 'userEnteredFormat.textFormat.bold,userEnteredFormat.backgroundColor'
            }},
            {'updateSheetProperties': {
                'properties': {'sheetId': sheet_id, 'gridProperties': {'frozenRowCount': 1}},
                'fields': 'gridProperties.frozenRowCount'
            }},
        ]}
    ).execute()


# ---- Tab data ------------------------------------------------------------------
#
# ONLINE_TARGETS
# Cols: Supervisor | Fee Monthly Target | Adm Monthly Target | Week Start | Week End
# Weekly target = monthly / (days_in_month/7)  -- computed by script
# Daily target  = monthly / days_in_month       -- computed by script
# Week Start/End = current active week. Update every Monday.
#
ONLINE_TARGETS_ROWS = [
    ['Supervisor', 'Fee Monthly Target', 'Adm Monthly Target', 'Week Start', 'Week End'],
    ['Varun',          5000000, 80, _monday, _sunday],
    ['Sunil',          5000000, 80, '',      ''     ],
    ['Siddarth Kumar', 5000000, 80, '',      ''     ],
    ['Vishal Gaur',    2000000, 40, '',      ''     ],
]

#
# ONLINE_COUNSELLORS
# Cols: Supervisor | Counsellor
# Counsellor fee targets are always 0 -- only supervisor targets matter.
#
ONLINE_COUNSELLORS_ROWS = [
    ['Supervisor', 'Counsellor'],
    ['Varun', 'Om  Sharma'],
    ['Varun', 'Sumit Yadav'],
    ['Varun', 'Tanisha Kulshrestha'],
    ['Varun', 'Mousumi Khatun'],
    ['Varun', 'Mayank Jain'],
    ['Varun', 'Divya Goyal'],
    ['Varun', 'Himanshi Baliyan'],
    ['Varun', 'Vijay Kumar'],
    ['Varun', 'Prashant'],
    ['Sunil', 'Preeti Lohiya'],
    ['Sunil', 'Vishwajeet'],
    ['Sunil', 'Tanya Sharma'],
    ['Sunil', 'Abhishek Swami'],
    ['Sunil', 'Vikas'],
    ['Sunil', 'Amit Kumar'],
    ['Sunil', 'Talveen'],
    ['Sunil', 'Avneet'],
    ['Sunil', 'Himanshi'],
    ['Siddarth Kumar', 'Tanya Singh'],
    ['Siddarth Kumar', 'Laxminaryan'],
    ['Siddarth Kumar', 'Suhani Raj Gupta'],
    ['Siddarth Kumar', 'Shubham Kumar Gupta'],
    ['Siddarth Kumar', 'Arnav Upadhyay'],
    ['Siddarth Kumar', 'Virat Kumar Singh'],
    ['Siddarth Kumar', 'Virat kumar singh'],
    ['Siddarth Kumar', 'Siddharth Kumar'],
    ['Vishal Gaur', 'Shivangi Bhurji'],
    ['Vishal Gaur', 'Riya Siddiqui'],
    ['Vishal Gaur', 'Ankita Singh Chauhan'],
    ['Vishal Gaur', 'Prerna Jain'],
    ['Vishal Gaur', 'Vishwajeet'],
    ['Vishal Gaur', 'Amit Kumar'],
    ['Vishal Gaur', 'Arun Gathe'],
]

#
# REGULAR_TARGETS
# Cols: College | Month Start | Month End | Week Start | Week End |
#       Adm Monthly Target | Forms Monthly Target
#
# Script computes:
#   Week target = Monthly / (days_in_month / 7)
#   FTD target  = Monthly / days_in_month
#
# Month Start/End: first and last day of current month.
# Week Start/End:  current active week. Update every Monday.
# Dates only needed in the first data row -- rest can be blank.
#
REGULAR_TARGETS_ROWS = [
    ['College', 'Month Start', 'Month End', 'Week Start', 'Week End',
     'Adm Monthly Target', 'Forms Monthly Target'],
    ['Amity University (All Campuses)',
     _month_start, _month_end, _monday, _sunday, 120, 1200],
    ['Lovely Professional University',
     '', '', '', '', 94, 409],
    ['Chandigarh University, Mohali',
     '', '', '', '', 127, 423],
    ['Chandigarh Group of Colleges, Landran (CGC)',
     '', '', '', '', 68, 272],
    ['Chandigarh University, Lucknow',
     '', '', '', '', 75, 250],
]

#
# REPORT_LOGS  -- appended by script after each run
# Cols: Date | Report | Grand Total Summary | WhatsApp Sent | Run At (UTC)
#
REPORT_LOGS_ROWS = [
    ['Date', 'Report', 'Grand Total Summary', 'WhatsApp Sent', 'Run At (UTC)'],
]


# ---- Main ----------------------------------------------------------------------
def main():
    print("=" * 58)
    print("  Google Sheets Setup  --  LMS Reports")
    print("=" * 58)
    print(f"  Sheet: {SPREADSHEET_ID}\n")

    svc      = get_service()
    existing = get_existing_tabs(svc)
    print(f"  Current tabs: {list(existing.keys())}\n")

    tabs = [
        ('Online_Targets',     ONLINE_TARGETS_ROWS,     5),
        ('Online_Counsellors', ONLINE_COUNSELLORS_ROWS, 2),
        ('Regular_Targets',    REGULAR_TARGETS_ROWS,    7),
        ('Report_Logs',        REPORT_LOGS_ROWS,        5),
    ]

    for tab_name, rows, ncols in tabs:
        print(f"  --- {tab_name} ---")
        sheet_id = ensure_tab(svc, tab_name, existing)
        write_tab(svc, tab_name, rows)
        format_tab(svc, sheet_id, ncols)
        print(f"  Written: {len(rows) - 1} data row(s)\n")

    print("=" * 58)
    print("  Done!")
    print()
    print("  HOW TARGETS WORK:")
    print("    Store only Monthly targets in the sheet.")
    print("    Script auto-computes:")
    print("      Week target = Monthly / (days_in_month / 7)")
    print("      FTD  target = Monthly / days_in_month")
    print()
    print("  UPDATE EACH WEEK (every Monday):")
    print("    Online_Targets  row 2, cols D+E  (Week Start/End)")
    print("    Regular_Targets row 2, cols D+E  (Week Start/End)")
    print()
    print("  UPDATE EACH MONTH (on the 1st):")
    print("    Both tabs: update Month Start/End and all Monthly Targets")
    print("=" * 58)


if __name__ == '__main__':
    main()
