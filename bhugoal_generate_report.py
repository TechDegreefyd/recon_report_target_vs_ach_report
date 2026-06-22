#!/usr/bin/env python3
"""
Bhugoal Daily Report Generator
1. Fetches all 7 APIs
2. Injects data into HTML template
3. Screenshots each section via Playwright
4. Sends screenshots to WhatsApp group

Usage:
  python bhugoal_generate_report.py          # full run
  python bhugoal_generate_report.py --local  # skip WhatsApp send
"""

import json
import os
import sys
import shutil
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

_SCRIPT_DIR = Path(__file__).parent
load_dotenv(_SCRIPT_DIR / '.env')

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

LOCAL_MODE = '--local' in sys.argv

# ─── DATES ───────────────────────────────────────────────────────────────────
_IST        = timezone(timedelta(hours=5, minutes=30))
_now        = datetime.now(_IST)
YESTERDAY   = (_now - timedelta(days=1)).strftime('%Y-%m-%d')
LAST_7_FROM = (_now - timedelta(days=7)).strftime('%Y-%m-%d')
MTD_FROM    = _now.replace(day=1).strftime('%Y-%m-%d')
RANGE_FROM  = os.getenv('BHUGOAL_RANGE_FROM_DATE', YESTERDAY)

def _fmt(d):
    dt = datetime.strptime(d, '%Y-%m-%d')
    day = str(dt.day)
    return f"{day} {dt.strftime('%b %Y')}"

DATES = {
    "yesterday":  _fmt(YESTERDAY),
    "last7From":  _fmt(LAST_7_FROM),
    "mtdFrom":    _fmt(MTD_FROM),
    "rangeFrom":  _fmt(RANGE_FROM),
    "monthName":  _now.strftime('%B'),
    "year":       str(_now.year),
}

# ─── ENDPOINTS ───────────────────────────────────────────────────────────────
_PRODUCTIVITY         = os.getenv('BHUGOAL_API_PRODUCTIVITY_REPORT')
_STUDENTREMARKS       = os.getenv('BHUGOAL_API_STUDENTREMARKS_REPORT')
_LOGIN_EMAIL          = os.getenv('BHUGOAL_LOGIN_EMAIL')
_LOGIN_PASSWORD       = os.getenv('BHUGOAL_LOGIN_PASSWORD')
_LOGIN_ROLE           = os.getenv('BHUGOAL_LOGIN_ROLE', 'admin')
_LEAD_STATUS          = os.getenv('BHUGOAL_API_LEAD_STATUS_REPORT')
_LEAD_FUNNEL          = os.getenv('BHUGOAL_API_LEAD_FUNNEL_REPORT')
_APPOINTMENT          = os.getenv('BHUGOAL_API_APPOINTMENT_FUNNEL_REPORT')

WHAPI_TOKEN    = os.getenv('WHAPI_TOKEN_PAID')
BHUGOAL_GROUP  = os.getenv('WHATSAPP_GROUP_BHUGOAL')

# ─── FRESH LEADS AGING CONFIG ────────────────────────────────────────────────
# Column that is blank for uncontacted / fresh leads
_AGING_STATUS_COL   = 'Lead Status'
_AGING_COUNSELOR    = 'L1 Counsellor Name'
_AGING_CREATED_COL  = 'Registration Date'
_AGING_DOWNLOAD_URL = (
    'https://api-v2-sales.bhugoal.ai/api/bhugoalUser/auth/download-users'
    '?tab=dashboard&type=total'
)

# ─── ACTIVE COUNSELLOR REMARKS CONFIG ────────────────────────────────────────
_ACTIVE_STATUSES = {
    'Counselling yet to be done',
    'Initial Counseling Completed',
    'Loan Processing',
    'Meeting Attended',
    'Meeting Not Attended',
    'Meeting Scheduled',
}
_REMARKS_COL     = 'Total Remarks'   # column holding remark count per lead
_REMARKS_BUCKETS = [
    ('1-2',  1,  2),
    ('3-5',  3,  5),
    ('6-7',  6,  7),
    ('7+',   8,  None),
]

# ─── OUTPUT PATHS ────────────────────────────────────────────────────────────
OUTPUT_DIR      = _SCRIPT_DIR / 'Automation Cron Job' / 'Target Report'
SCREENSHOT_DIR  = OUTPUT_DIR / 'bhugoal_screenshots'
HTML_OUTPUT     = OUTPUT_DIR / 'bhugoal_daily_report.html'
TEMPLATE_PATH   = _SCRIPT_DIR / 'bhugoal_daily_report_template.html'

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

# ─── API HELPERS ─────────────────────────────────────────────────────────────
def get_auth_token():
    r = requests.post(
        "https://api-v2-sales.bhugoal.ai/api/bhugoalLmsUser/auth/login",
        json={"email": _LOGIN_EMAIL, "password": _LOGIN_PASSWORD, "role": _LOGIN_ROLE},
        timeout=15,
    )
    r.raise_for_status()
    token = r.json().get("token")
    if not token:
        raise RuntimeError(f"Login failed: {r.text[:200]}")
    return token

def _fetch(url, params, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = requests.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()

def _paged(sub_tab, from_date, to_date):
    return {
        "page": 1, "limit": 30, "subTab": sub_tab, "program": "all",
        "fromDate": from_date, "toDate": to_date,
        "createdFrom": from_date, "createdTo": to_date,
        "startDate": from_date, "endDate": to_date,
    }

# ─── FRESH LEADS AGING ───────────────────────────────────────────────────────
_AGING_EMPTY = {"counselors": [], "rows": [], "totalFresh": 0, "asOf": ""}

def _parse_download_response(r):
    """Try to parse an API response as Excel, then CSV, then JSON. Returns a DataFrame."""
    import io
    ct = r.headers.get('Content-Type', '')

    # ── Excel (binary) ───────────────────────────────────────────────────────
    if 'spreadsheet' in ct or 'excel' in ct or 'octet-stream' in ct or r.content[:4] == b'PK\x03\x04':
        print(f"  [Aging API] Detected Excel binary ({ct})")
        return pd.read_excel(io.BytesIO(r.content))

    # ── CSV ──────────────────────────────────────────────────────────────────
    text = r.text.strip()
    if 'csv' in ct or (text and ',' in text.splitlines()[0] and not text.startswith('{')):
        print(f"  [Aging API] Detected CSV ({ct})")
        return pd.read_csv(io.StringIO(text))

    # ── JSON ─────────────────────────────────────────────────────────────────
    payload = r.json()
    if isinstance(payload, list):
        return pd.DataFrame(payload)
    for key in ('data', 'users', 'leads', 'result'):
        if key in payload and isinstance(payload[key], list):
            return pd.DataFrame(payload[key])
    raise ValueError(f"Unknown JSON shape — keys: {list(payload.keys()) if isinstance(payload, dict) else type(payload)}")


def fetch_fresh_leads_aging(token):
    """Download all leads and return a pivot of fresh-lead ages × counselors."""
    headers = {'Authorization': f'Bearer {token}'}
    r = requests.get(_AGING_DOWNLOAD_URL, headers=headers, timeout=60)

    print(f"  [Aging API] status={r.status_code}  content-type={r.headers.get('Content-Type','?')}  size={len(r.content)} bytes")

    if not r.ok:
        print(f"  [Aging API] HTTP error: {r.text[:300]}")
        return _AGING_EMPTY

    try:
        df = _parse_download_response(r)
    except Exception as e:
        print(f"  [Aging API] Could not parse response: {e}")
        return _AGING_EMPTY

    print(f"  [Aging API] {len(df)} rows loaded. Columns: {list(df.columns)}")

    if df.empty:
        print("  [Aging API] Empty dataframe — skipping.")
        return _AGING_EMPTY

    if _AGING_STATUS_COL not in df.columns:
        print(f"  [WARN] Column '{_AGING_STATUS_COL}' not found. Available: {list(df.columns)}")
        return _AGING_EMPTY

    # Fresh leads = Lead Status is blank
    fresh = df[df[_AGING_STATUS_COL].isna() | (df[_AGING_STATUS_COL].astype(str).str.strip() == '')].copy()

    # Date format from API: "11/06/2026, 04:41 PM"  (DD/MM/YYYY, HH:MM AM/PM)
    fresh[_AGING_CREATED_COL] = pd.to_datetime(
        fresh[_AGING_CREATED_COL], format='%d/%m/%Y, %I:%M %p', errors='coerce'
    )
    cutoff = pd.to_datetime(RANGE_FROM)
    fresh = fresh[fresh[_AGING_CREATED_COL] >= cutoff]

    print(f"  Fresh leads : {len(fresh)}  (blank Lead Status, registered >= {RANGE_FROM})")

    if fresh.empty:
        print("  [Aging] No fresh leads found.")
        return _AGING_EMPTY

    if _AGING_CREATED_COL not in fresh.columns:
        print(f"  [WARN] Created-date column '{_AGING_CREATED_COL}' not found. Available: {list(fresh.columns)}")
        return _AGING_EMPTY

    now_naive = _now.replace(tzinfo=None)
    def _age(val):
        if pd.isna(val):
            return None
        try:
            dt = val if isinstance(val, pd.Timestamp) else pd.to_datetime(val)
            return max(0, (now_naive - dt.replace(tzinfo=None)).days)
        except Exception:
            return None

    fresh['_age'] = fresh[_AGING_CREATED_COL].apply(_age)
    fresh = fresh[fresh['_age'].notna()]
    fresh['_age'] = fresh['_age'].astype(int)

    if _AGING_COUNSELOR not in fresh.columns:
        fresh['_counselor'] = 'Unassigned'
    else:
        fresh['_counselor'] = fresh[_AGING_COUNSELOR].fillna('Unassigned').astype(str).str.strip()

    pivot = fresh.pivot_table(
        index='_age', columns='_counselor',
        values=_AGING_CREATED_COL, aggfunc='count', fill_value=0,
    )
    pivot = pivot.sort_index(ascending=False)
    pivot['Grand Total'] = pivot.sum(axis=1)
    counselors = [c for c in pivot.columns if c != 'Grand Total']

    rows = []
    for age_days, row in pivot.iterrows():
        label = f'{age_days} Day' if age_days == 0 else f'{age_days} Days'
        entry = {'age': label}
        for c in counselors:
            entry[c] = int(row.get(c, 0))
        entry['Grand Total'] = int(row['Grand Total'])
        rows.append(entry)

    # totals row
    totals = {'age': 'Total'}
    for c in counselors:
        totals[c] = int(pivot[c].sum())
    totals['Grand Total'] = int(pivot['Grand Total'].sum())
    rows.append(totals)

    return {
        "counselors": counselors,
        "rows": rows,
        "totalFresh": int(totals['Grand Total']),
        "asOf": _now.strftime('%d %b %Y, %I:%M %p IST'),
    }

# ─── ACTIVE COUNSELLOR REMARKS ───────────────────────────────────────────────
_ACTIVE_EMPTY = {"buckets": [], "rows": [], "totalActive": 0, "asOf": ""}

def fetch_active_counsellor_remarks(token, df_cache=None):
    """Pivot of counselor × remarks bucket (active leads) + Fresh Leads + Not Interested + Grand Total."""
    if df_cache is None:
        headers = {'Authorization': f'Bearer {token}'}
        r = requests.get(_AGING_DOWNLOAD_URL, headers=headers, timeout=60)
        print(f"  [Active API] status={r.status_code}  content-type={r.headers.get('Content-Type','?')}  size={len(r.content)} bytes")
        if not r.ok:
            print(f"  [Active API] HTTP error: {r.text[:300]}")
            return _ACTIVE_EMPTY
        try:
            df = _parse_download_response(r)
        except Exception as e:
            print(f"  [Active API] Could not parse response: {e}")
            return _ACTIVE_EMPTY
    else:
        df = df_cache

    print(f"  [Active API] {len(df)} rows.")

    if _AGING_STATUS_COL not in df.columns:
        print(f"  [WARN] '{_AGING_STATUS_COL}' not found.")
        return _ACTIVE_EMPTY

    # Filter by registration date >= RANGE_FROM (same cutoff as fresh leads aging)
    df[_AGING_CREATED_COL] = pd.to_datetime(
        df[_AGING_CREATED_COL], format='%d/%m/%Y, %I:%M %p', errors='coerce'
    )
    cutoff = pd.to_datetime(RANGE_FROM)
    df = df[df[_AGING_CREATED_COL] >= cutoff].copy()
    print(f"  [Active API] {len(df)} rows after >= {RANGE_FROM} filter.")

    status_col = df[_AGING_STATUS_COL].astype(str).str.strip()

    # Segment the full dataset by counselor
    if _AGING_COUNSELOR not in df.columns:
        df['_counselor'] = 'Unassigned'
    else:
        df['_counselor'] = df[_AGING_COUNSELOR].fillna('Unassigned').astype(str).str.strip()

    # Fresh leads = blank Lead Status
    fresh_mask = df[_AGING_STATUS_COL].isna() | (status_col == '') | (status_col == 'nan')
    fresh_counts = df[fresh_mask].groupby('_counselor').size().rename('Fresh Leads')

    # Not Interested
    ni_counts = df[status_col == 'Not Interested'].groupby('_counselor').size().rename('Not Interested')

    # Active = Lead Status in the active set
    active = df[status_col.isin(_ACTIVE_STATUSES)].copy()
    print(f"  Active leads: {len(active)}  |  Fresh: {fresh_mask.sum()}  |  Not Interested: {(status_col == 'Not Interested').sum()}")

    if active.empty:
        return _ACTIVE_EMPTY

    if _REMARKS_COL not in active.columns:
        print(f"  [WARN] Remarks column '{_REMARKS_COL}' not found. Available: {list(active.columns)}")
        return _ACTIVE_EMPTY

    active['_remarks'] = pd.to_numeric(active[_REMARKS_COL], errors='coerce').fillna(0).astype(int)

    def _bucket(n):
        for label, lo, hi in _REMARKS_BUCKETS:
            if hi is None:
                if n >= lo:
                    return label
            elif lo <= n <= hi:
                return label
        return '1-2'

    active['_bucket'] = active['_remarks'].apply(_bucket)

    bucket_labels = [b[0] for b in _REMARKS_BUCKETS]

    pivot = (
        active.groupby(['_counselor', '_bucket'])
        .size()
        .unstack(fill_value=0)
        .reindex(columns=bucket_labels, fill_value=0)
    )
    pivot['Active Total'] = pivot.sum(axis=1)

    # Merge fresh + not-interested counts into the pivot
    pivot = pivot.join(fresh_counts, how='outer').join(ni_counts, how='outer').fillna(0)
    for col in bucket_labels + ['Active Total', 'Fresh Leads', 'Not Interested']:
        pivot[col] = pivot[col].astype(int)

    pivot['Grand Total'] = pivot['Active Total'] + pivot['Fresh Leads'] + pivot['Not Interested']
    pivot = pivot.sort_index()

    rows = []
    all_cols = bucket_labels + ['Active Total', 'Fresh Leads', 'Not Interested', 'Grand Total']
    for counselor, row in pivot.iterrows():
        entry = {'counselor': counselor}
        for col in all_cols:
            entry[col] = int(row.get(col, 0))
        rows.append(entry)

    # Grand Total row
    totals = {'counselor': 'Grand Total'}
    for col in all_cols:
        totals[col] = int(pivot[col].sum())
    rows.append(totals)

    return {
        "buckets": bucket_labels,
        "rows": rows,
        "totalActive": int(pivot['Active Total'].sum()),
        "totalFresh": int(pivot['Fresh Leads'].sum()),
        "totalNI": int(pivot['Not Interested'].sum()),
        "grandTotal": int(pivot['Grand Total'].sum()),
        "asOf": _now.strftime('%d %b %Y, %I:%M %p IST'),
    }


_TODAY = _now.strftime('%Y-%m-%d')

def _filter_today(table_data):
    """Remove any rows from the API response that belong to today (partial data)."""
    if not isinstance(table_data, list):
        return table_data
    today_variants = {
        _TODAY,                                          # 2026-06-22
        _now.strftime('%d/%m/%Y'),                       # 22/06/2026
        _now.strftime('%-d/%-m/%Y') if hasattr(_now, 'strftime') else '',  # 22/6/2026
        f"{_now.day}/{_now.month}/{_now.year}",         # 22/6/2026
    }
    def row_has_today(row):
        return any(str(v) in today_variants for v in row.values()) if isinstance(row, dict) else False
    return [r for r in table_data if not row_has_today(r)]


# ─── FETCH ALL ───────────────────────────────────────────────────────────────
def fetch_all_data():
    print("  Logging in to get auth token...")
    token = get_auth_token()
    print("  Auth token refreshed.\n")

    print("  Fetching Productivity Report...")
    productivity = _fetch(_PRODUCTIVITY, {"fromDate": YESTERDAY, "toDate": YESTERDAY})

    print("  Fetching Hourly Remarks Breakdown...")
    hourly_raw = _fetch(_STUDENTREMARKS, _paged("hourly-breakdown", YESTERDAY, YESTERDAY), token=token)

    print("  Fetching Lead Status — Yesterday...")
    ls_yday = _fetch(_LEAD_STATUS, _paged("l1_current_status_l1", YESTERDAY, YESTERDAY))

    print("  Fetching Lead Status — MTD...")
    ls_range = _fetch(_LEAD_STATUS, _paged("l1_current_status_l1", MTD_FROM, YESTERDAY))

    print("  Fetching Lead Funnel — Last 7 Days (Date Wise)...")
    funnel_date = _fetch(_LEAD_FUNNEL, _paged("leadfunnel_datewise", LAST_7_FROM, YESTERDAY))

    print("  Fetching Campaign Funnel — Range...")
    funnel_range = _fetch(_LEAD_FUNNEL, _paged("leadfunnel_campaign", RANGE_FROM, YESTERDAY))

    print("  Fetching Campaign Funnel — MTD...")
    funnel_mtd = _fetch(_LEAD_FUNNEL, _paged("leadfunnel_campaign", MTD_FROM, YESTERDAY))

    print("  Fetching Appointment Status — MTD...")
    appointment = _fetch(_APPOINTMENT, _paged("appointment_status", MTD_FROM, YESTERDAY))

    print("  Fetching Fresh Leads Aging Report...")
    try:
        fresh_leads_aging = fetch_fresh_leads_aging(token)
    except Exception as e:
        print(f"  [WARN] Fresh leads aging failed: {e}")
        fresh_leads_aging = _AGING_EMPTY

    print("  Fetching Active Counsellor Remarks Report...")
    try:
        active_counsellor_remarks = fetch_active_counsellor_remarks(token)
    except Exception as e:
        print(f"  [WARN] Active counsellor remarks failed: {e}")
        active_counsellor_remarks = _ACTIVE_EMPTY

    return {
        "dates": DATES,
        "productivity": productivity.get("data", []),
        "hourlyBreakdown": {
            "tableData":   hourly_raw["data"]["tableData"],
            "counselors":  hourly_raw["data"].get("counselors", []),
            "grandTotals": hourly_raw["data"].get("grandTotals", {}),
            "summary":     hourly_raw["summary"],
        },
        "leadStatusYesterday": {
            "tableData": ls_yday["data"]["tableData"],
            "allStages": ls_yday["data"].get("allStatuses", []),
            "summary":   ls_yday["summary"],
        },
        "leadStatusRange": {
            "tableData": ls_range["data"]["tableData"],
            "allStages": ls_range["data"].get("allStatuses", []),
            "summary":   ls_range["summary"],
        },
        "funnelDateWise": {
            "tableData": _filter_today(funnel_date["data"]["tableData"]),
            "summary":   funnel_date["summary"],
        },
        "funnelRange": {
            "tableData": funnel_range["data"]["tableData"],
            "summary":   funnel_range["summary"],
        },
        "funnelMTD": {
            "tableData": funnel_mtd["data"]["tableData"],
            "summary":   funnel_mtd["summary"],
        },
        "appointment": {
            "tableData": appointment["data"]["tableData"],
            "summary":   appointment["summary"],
        },
        "freshLeadsAging": fresh_leads_aging,
        "activeCounsellorRemarks": active_counsellor_remarks,
    }

# ─── GENERATE HTML ───────────────────────────────────────────────────────────
def generate_html(data):
    template = TEMPLATE_PATH.read_text(encoding='utf-8')
    html = template.replace('%%REPORT_DATA%%', json.dumps(data, ensure_ascii=False))
    HTML_OUTPUT.write_text(html, encoding='utf-8')
    print(f"  HTML saved → {HTML_OUTPUT}")
    return HTML_OUTPUT

# ─── SCREENSHOT ──────────────────────────────────────────────────────────────
SECTION_LABELS = [
    ("s1", "Yesterday's Hourly Remarks - Counsellor Wise"),
    ("s2", "Yesterday's Lead Status - Counsellor Wise"),
    ("s3", "MTD Lead Status - Counsellor Wise"),
    ("s4", "Last 7 Days - Lead Funnel - DOD"),
    ("s5", "YTD Campaign Wise Lead Funnel"),
    ("s6", "MTD Campaign Wise Lead Funnel"),
    ("s7", "MTD - Appointment Current Status Report"),
    ("s8", "Fresh Leads Aging Report"),
    ("s9", "Active Counsellor Remarks Report"),
]

def screenshot_sections(html_path):
    from playwright.sync_api import sync_playwright

    screenshots = []
    html_uri = html_path.as_uri()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(html_uri)
        page.wait_for_timeout(800)

        # Hide fixed nav elements so section screenshots are clean
        page.evaluate("""() => {
            document.querySelector('.sidebar').style.display = 'none';
            document.querySelector('.top-header').style.display = 'none';
            document.querySelector('.main').style.marginLeft = '0';
            document.querySelector('.layout').style.marginTop = '0';
        }""")
        page.wait_for_timeout(200)

        for sid, label in SECTION_LABELS:
            el = page.query_selector(f'#{sid}')
            if el:
                path = SCREENSHOT_DIR / f'{sid}.png'
                el.screenshot(path=str(path))
                screenshots.append((str(path), label))
                print(f"  Screenshot saved → {path.name}")
            else:
                print(f"  [SKIP] #{sid} not found in page")

        browser.close()

    return screenshots

# ─── WHATSAPP SEND ───────────────────────────────────────────────────────────
def _send_image(image_path, caption, group_id):
    headers = {"Authorization": f"Bearer {WHAPI_TOKEN}"}
    with open(image_path, 'rb') as f:
        for attempt in range(2):
            try:
                r = requests.post(
                    "https://gate.whapi.cloud/messages/image",
                    headers=headers,
                    data={"to": group_id, "caption": caption},
                    files={"media": (os.path.basename(image_path), f, "image/png")},
                    timeout=30,
                )
                if 200 <= r.status_code < 300:
                    return True
                print(f"    [WHAPI {r.status_code}] {r.text[:120]}")
                f.seek(0)
            except requests.exceptions.Timeout:
                print(f"    [WHAPI TIMEOUT]")
            except Exception as e:
                print(f"    [WHAPI ERROR] {e}")
    return False

def send_to_whatsapp(screenshots):
    if not BHUGOAL_GROUP:
        print("  [SKIP] WHATSAPP_GROUP_BHUGOAL not set in .env")
        return
    if not WHAPI_TOKEN:
        print("  [SKIP] WHAPI_TOKEN_PAID not set in .env")
        return

    yesterday_str = DATES['yesterday']
    for path, label in screenshots:
        caption = f"*{label}*\n_{yesterday_str}_"
        ok = _send_image(path, caption, BHUGOAL_GROUP)
        print(f"  {'✅' if ok else '❌'} {label}")

# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    print(f"\nBhugoal Report Generator")
    print(f"Yesterday   : {YESTERDAY}  ({DATES['yesterday']})")
    print(f"Last 7 From : {LAST_7_FROM}")
    print(f"MTD From    : {MTD_FROM}")
    print(f"Range From  : {RANGE_FROM}")
    print(f"Local Mode  : {LOCAL_MODE}\n")

    print("[ 1 / 4 ] Fetching APIs...")
    data = fetch_all_data()
    print("  All APIs fetched.\n")

    print("[ 2 / 4 ] Generating HTML...")
    html_path = generate_html(data)
    print()

    print("[ 3 / 4 ] Taking screenshots...")
    screenshots = screenshot_sections(html_path)
    print(f"  {len(screenshots)} screenshots taken.\n")

    if LOCAL_MODE:
        print("[ 4 / 4 ] Local mode — skipping WhatsApp send.")
        print(f"  HTML  : {html_path}")
        print(f"  PNGs  : {SCREENSHOT_DIR}")
    else:
        print("[ 4 / 4 ] Sending to WhatsApp...")
        send_to_whatsapp(screenshots)

        print("\n[ Cleanup ] Removing screenshots and HTML...")
        removed = 0
        try:
            if HTML_OUTPUT.exists():
                HTML_OUTPUT.unlink()
                removed += 1
            if SCREENSHOT_DIR.exists():
                shutil.rmtree(SCREENSHOT_DIR)
                removed += 1
            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"  [WARN] Cleanup error: {e}")
        print(f"  Cleaned {removed} item(s).")

    print("\nDone.")

if __name__ == "__main__":
    main()
