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
    — Roster only; per-counsellor fee targets live in the Counsellor Targets tab (gid below)

  Counsellor WIse Targets   (gid=299650822, A:C)  — tab name has trailing space
    A: Supervisor  B: Counsellor  C: Fee Target (number or '-' for no target)
    — New counsellors are auto-appended with '-' as target.
    — Used in the "Counsellor T vs A" report tab.

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

# gid of the per-counsellor fee-targets tab
_COUNSELLOR_TARGETS_GID = 299650822
_counsellor_targets_tab_name = None   # resolved once on first use

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

    # Prefer env-var credentials (CI / deployed) over local files
    token_env = os.environ.get('GOOGLE_TOKEN_JSON')
    client_secret_env = os.environ.get('GOOGLE_CLIENT_SECRET_JSON')

    if token_env:
        creds = Credentials.from_authorized_user_info(json.loads(token_env), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Persist refreshed token back to env-var path or file
            if token_env:
                # Write refreshed token to file so next run picks it up
                with open(_TOKEN_PATH, 'w') as fh:
                    fh.write(creds.to_json())
        else:
            # Fall back to file-based flow
            if os.path.exists(_TOKEN_PATH):
                creds = Credentials.from_authorized_user_file(_TOKEN_PATH, SCOPES)
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    with open(_TOKEN_PATH, 'w') as fh:
                        fh.write(creds.to_json())
            else:
                if not os.path.exists(_CLIENT_SECRET) and client_secret_env:
                    with open(_CLIENT_SECRET, 'w') as fh:
                        fh.write(client_secret_env)
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


import re as _re

def _norm(name: str) -> str:
    """Canonical name: collapse internal spaces, strip, title-case."""
    return _re.sub(r'\s+', ' ', str(name)).strip().title()


def _get_counsellor_targets_tab() -> str:
    """Resolve gid → sheet title once per process, cache the result."""
    global _counsellor_targets_tab_name
    if _counsellor_targets_tab_name:
        return _counsellor_targets_tab_name
    svc = get_service()
    meta = svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    for sheet in meta.get('sheets', []):
        props = sheet.get('properties', {})
        if props.get('sheetId') == _COUNSELLOR_TARGETS_GID:
            _counsellor_targets_tab_name = props['title']
            return _counsellor_targets_tab_name
    raise ValueError(
        f"No sheet with gid={_COUNSELLOR_TARGETS_GID} found in spreadsheet {SPREADSHEET_ID}. "
        "Create the tab with columns: A=Supervisor, B=Counsellor, C=Fee Target."
    )


def load_counsellor_fee_targets() -> dict:
    """
    Read per-counsellor fee targets from the Counsellor Targets tab (gid=299650822).

    Expected sheet layout (A:C, row 1 = header):
      A: Supervisor   B: Name (counsellor)   C: Sum of Target <month>

    Supervisor-total rows (Name == "Total") and the Grand Total row are skipped.
    Returns {counsellor_name_normalised: int_or_None}
      — None means no target set ('-' / blank in column C).
    """
    tab = _get_counsellor_targets_tab()
    rows = _read_range(f"'{tab}'!A2:C2000")
    targets = {}
    sup_map = {}   # counsellor_name → supervisor_name (both normalised)
    current_sup = ''
    for row in rows:
        if len(row) < 2:
            continue
        sup_raw  = row[0].strip() if row[0].strip() else current_sup
        name_raw = row[1].strip()
        if not name_raw or name_raw.lower() in ('total', 'grand total'):
            if sup_raw:
                current_sup = sup_raw
            continue
        if sup_raw:
            current_sup = sup_raw
        couns = _norm(name_raw)
        sup   = _norm(current_sup)
        raw   = row[2].strip() if len(row) > 2 else ''
        targets[couns] = None if (raw in ('-', '', '—') or raw.lower() == 'none') else _int(raw)
        if sup:
            sup_map[couns] = sup
    return targets, sup_map


# ─── Online config ──────────────────────────────────────────────────────────────

def load_online_config():
    """
    Returns:
    {
      "supervisor_targets":            {sup: fee_target, ...},       — from Online_Targets
      "supervisor_admission_targets":  {sup: adm_target, ...},       — from Online_Targets
      "counsellor_fee_targets":        {couns_name: int_or_None, ...} — flat dict from Counsellor Wise Targets tab
    }
    Roster (which counsellors exist under which supervisor) is fetched live from the DB
    in online_get_data(), so Online_Counsellors sheet is no longer used.
    """
    # --- Online_Targets (supervisor-level targets) ---
    rows = _read_range('Online_Targets!A2:C200')
    supervisor_targets = {}
    supervisor_admission_targets = {}
    for row in rows:
        if not row or not row[0].strip():
            continue
        sup = _norm(row[0])
        supervisor_targets[sup] = _int(row[1]) if len(row) > 1 else 0
        supervisor_admission_targets[sup] = _int(row[2]) if len(row) > 2 else 0

    # --- Per-counsellor fee targets + supervisor map (Counsellor Wise Targets tab) ---
    counsellor_fee_targets = {}
    counsellor_supervisor_map = {}   # {couns_name: sup_name} for supplementing DB roster
    try:
        counsellor_fee_targets, counsellor_supervisor_map = load_counsellor_fee_targets()
    except Exception as e:
        print(f"  ⚠️  Could not load per-counsellor fee targets (non-fatal): {e}")

    return {
        "supervisor_targets": supervisor_targets,
        "supervisor_admission_targets": supervisor_admission_targets,
        "counsellor_fee_targets": counsellor_fee_targets,
        "counsellor_supervisor_map": counsellor_supervisor_map,
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


# ─── Auto-detect missing counsellors ────────────────────────────────────────────

async def sync_online_counsellors(online_db: dict):
    """
    Compares active counsellors in the DB (via assigned_to) against the
    Online_Counsellors sheet. Appends any missing ones under their supervisor
    with a blank target row so they're tracked in the next report run.

    online_db: the ONLINE_DB dict (host/port/database/user/password).
    Returns list of (supervisor_name, counsellor_name) tuples that were added.
    """
    import asyncpg

    conn = await asyncpg.connect(**online_db)

    sup_ids = await conn.fetch("""
        SELECT counsellor_id, counsellor_name
        FROM counsellors
        WHERE status = 'active'
    """)
    id_to_name = {r['counsellor_id']: _norm(r['counsellor_name']) for r in sup_ids}

    db_rows = await conn.fetch("""
        SELECT counsellor_name, assigned_to
        FROM counsellors
        WHERE status = 'active'
          AND assigned_to IS NOT NULL
          AND assigned_to != '[default]'
    """)
    await conn.close()

    # Build supervisor_name → set of counsellor names from DB
    db_mapping = {}
    for r in db_rows:
        sup_id = r['assigned_to']
        sup_name = id_to_name.get(sup_id, '')
        couns_name = _norm(r['counsellor_name'])
        if sup_name and couns_name and sup_name != couns_name:
            db_mapping.setdefault(sup_name, set()).add(couns_name)

    # Read current sheet contents
    existing_rows = _read_range('Online_Counsellors!A2:B500')
    existing = set()
    for row in existing_rows:
        if len(row) >= 2 and row[0].strip() and row[1].strip():
            existing.add((_norm(row[0]), _norm(row[1])))

    # Also read supervisors from Online_Targets to know which ones we track
    target_rows = _read_range('Online_Targets!A2:A200')
    tracked_supervisors = {_norm(r[0]) for r in target_rows if r and r[0].strip()}

    # Find counsellors in DB but missing from sheet, for tracked supervisors only
    to_add = []
    for sup_name, couns_set in sorted(db_mapping.items()):
        if sup_name not in tracked_supervisors:
            continue
        for couns_name in sorted(couns_set):
            if (sup_name, couns_name) not in existing:
                to_add.append([sup_name, couns_name])

    if to_add:
        svc = get_service()
        svc.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range='Online_Counsellors!A:B',
            valueInputOption='USER_ENTERED',
            body={'values': to_add}
        ).execute()
        print(f"  [sync] Auto-added {len(to_add)} counsellor(s) to Online_Counsellors sheet:")
        for row in to_add:
            print(f"     + {row[1]}  (under {row[0]})")

        # Also append to Counsellor Targets tab with '-' as placeholder target.
        # Use the DB-derived supervisor name (already normalised to match Online_Targets)
        # so the new row is consistent with the rest of the sheet.
        try:
            tab = _get_counsellor_targets_tab()
            # Read existing counsellor names (column B) to avoid duplicates
            existing_tva = _read_range(f"'{tab}'!B2:B2000")
            existing_couns_set = {_norm(row[0]) for row in existing_tva if row and row[0].strip()}
            tva_rows = [
                [sup, couns, '-']
                for sup, couns in to_add
                if _norm(couns) not in existing_couns_set
            ]
            if tva_rows:
                svc.spreadsheets().values().append(
                    spreadsheetId=SPREADSHEET_ID,
                    range=f"'{tab}'!A:C",
                    valueInputOption='USER_ENTERED',
                    body={'values': tva_rows}
                ).execute()
                print(f"  [sync] Added {len(tva_rows)} counsellor(s) to '{tab}' with '-' target:")
                for row in tva_rows:
                    print(f"     + {row[1]}  (under {row[0]})  target='-'")
        except Exception as e:
            print(f"  ⚠️  Could not sync to Counsellor Targets tab (non-fatal): {e}")
    else:
        print("  [sync] Online_Counsellors sheet is up to date — no missing counsellors.")

    return [(r[0], r[1]) for r in to_add]


# ─── Auto-update Regular period dates ───────────────────────────────────────────

def sync_regular_dates(month_start: str, month_end: str, week_start: str, week_end: str):
    """
    Overwrites the period dates in Regular_Targets row 2 (cols B-E) with the
    current computed dates so the sheet always reflects the active reporting window.
    """
    svc = get_service()
    svc.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range='Regular_Targets!B2:E2',
        valueInputOption='USER_ENTERED',
        body={'values': [[month_start, month_end, week_start, week_end]]}
    ).execute()
    print(f"  [sync] Regular_Targets dates updated: month {month_start} -> {month_end}, week {week_start} -> {week_end}")


# ─── Outbound SIM Map ───────────────────────────────────────────────────────────

def load_outbound_sim_map() -> dict:
    """
    Read SIM → (Name, Team) mapping from Outbound_SIM_Map tab.

    Sheet layout (A:C, row 1 = header):
      A: SIM (10-digit number stored as text)   B: Name   C: Team

    Returns {sim_str: (name, team), ...}
    """
    rows = _read_range('Outbound_SIM_Map!A2:C2000')
    sim_map = {}
    for row in rows:
        if len(row) < 3 or not row[0].strip():
            continue
        sim  = row[0].strip()
        name = row[1].strip()
        team = row[2].strip()
        if sim and name and team:
            sim_map[sim] = (name, team)
        elif sim or name:
            print(f'  ⚠️  Outbound_SIM_Map: incomplete row skipped — SIM={sim!r} Name={name!r} Team={team!r}')
    return sim_map


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
