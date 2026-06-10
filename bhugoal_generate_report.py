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
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

_SCRIPT_DIR = Path(__file__).parent
load_dotenv(_SCRIPT_DIR / '.env')

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

LOCAL_MODE = '--local' in sys.argv

# ─── DATES ───────────────────────────────────────────────────────────────────
_now        = datetime.now()
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
_PRODUCTIVITY = os.getenv('BHUGOAL_API_PRODUCTIVITY_REPORT')
_LEAD_STATUS  = os.getenv('BHUGOAL_API_LEAD_STATUS_REPORT')
_LEAD_FUNNEL  = os.getenv('BHUGOAL_API_LEAD_FUNNEL_REPORT')
_APPOINTMENT  = os.getenv('BHUGOAL_API_APPOINTMENT_FUNNEL_REPORT')

WHAPI_TOKEN    = os.getenv('WHAPI_TOKEN')
BHUGOAL_GROUP  = os.getenv('WHATSAPP_GROUP_BHUGOAL')

# ─── OUTPUT PATHS ────────────────────────────────────────────────────────────
OUTPUT_DIR      = _SCRIPT_DIR / 'Automation Cron Job' / 'Target Report'
SCREENSHOT_DIR  = OUTPUT_DIR / 'bhugoal_screenshots'
HTML_OUTPUT     = OUTPUT_DIR / 'bhugoal_daily_report.html'
TEMPLATE_PATH   = _SCRIPT_DIR / 'bhugoal_daily_report_template.html'

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

# ─── API HELPERS ─────────────────────────────────────────────────────────────
def _fetch(url, params):
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def _paged(sub_tab, from_date, to_date):
    return {
        "page": 1, "limit": 30, "subTab": sub_tab, "program": "all",
        "fromDate": from_date, "toDate": to_date,
        "createdFrom": from_date, "createdTo": to_date,
        "startDate": from_date, "endDate": to_date,
    }

# ─── FETCH ALL ───────────────────────────────────────────────────────────────
def fetch_all_data():
    print("  Fetching Productivity Report...")
    productivity = _fetch(_PRODUCTIVITY, {"fromDate": YESTERDAY, "toDate": YESTERDAY})

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

    return {
        "dates": DATES,
        "productivity": productivity.get("data", []),
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
            "tableData": funnel_date["data"]["tableData"],
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
        print("  [SKIP] WHAPI_TOKEN not set in .env")
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
