#!/usr/bin/env python3
"""
Bhugoal Reports Automation — Data Fetch Script
Fetches all daily reports from the Bhugoal Sales API and prints the data.
"""

import requests
import json
import sys
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_SCRIPT_DIR, '.env'))

# ─── DATES ───────────────────────────────────────────────────────────────────
_now        = datetime.now()
YESTERDAY   = (_now - timedelta(days=1)).strftime('%Y-%m-%d')
LAST_7_FROM = (_now - timedelta(days=7)).strftime('%Y-%m-%d')
MTD_FROM    = _now.replace(day=1).strftime('%Y-%m-%d')
RANGE_FROM  = os.getenv('BHUGOAL_RANGE_FROM_DATE', YESTERDAY)  # e.g. 2026-05-15

# ─── ENDPOINT URLS ───────────────────────────────────────────────────────────
_PRODUCTIVITY  = os.getenv('BHUGOAL_API_PRODUCTIVITY_REPORT')
_LEAD_STAGE    = os.getenv('BHUGOAL_API_LEAD_STAGE_REPORT')
_LEAD_FUNNEL   = os.getenv('BHUGOAL_API_LEAD_FUNNEL_REPORT')
_APPOINTMENT   = os.getenv('BHUGOAL_API_APPOINTMENT_FUNNEL_REPORT')

# ─── PARAM BUILDERS ──────────────────────────────────────────────────────────
def _date_params(from_date: str, to_date: str) -> dict:
    return {
        "fromDate":    from_date,
        "toDate":      to_date,
        "createdFrom": from_date,
        "createdTo":   to_date,
        "startDate":   from_date,
        "endDate":     to_date,
    }

def _paged_params(sub_tab: str, from_date: str, to_date: str) -> dict:
    return {
        "page":    1,
        "limit":   30,
        "subTab":  sub_tab,
        "program": "all",
        **_date_params(from_date, to_date),
    }

# ─── ENDPOINTS ───────────────────────────────────────────────────────────────
# Format: "Report Name": (url, params_dict)
ENDPOINTS = {

    # ── Productivity ─────────────────────────────────────────────────────────
    "Productivity Report — Yesterday": (
        _PRODUCTIVITY,
        _date_params(YESTERDAY, YESTERDAY),
    ),

    # ── Lead Status ──────────────────────────────────────────────────────────
    "Lead Status Counsellor L1 Wise — Yesterday": (
        _LEAD_STATUS,
        _paged_params("l1_current_status_l1", YESTERDAY, YESTERDAY),
    ),

    "Lead Status Counsellor L1 Wise — MTD": (
        _LEAD_STATUS,
        _paged_params("l1_current_status_l1", MTD_FROM, YESTERDAY),
    ),

    # ── Lead Funnel ──────────────────────────────────────────────────────────
    "Lead Funnel Date Wise — Last 7 Days": (
        _LEAD_FUNNEL,
        _paged_params("leadfunnel_datewise", LAST_7_FROM, YESTERDAY),
    ),

    # ── Campaign Funnel ──────────────────────────────────────────────────────
    "Campaign Wise Funnel Report — 15 May to Yesterday": (
        _LEAD_FUNNEL,
        _paged_params("leadfunnel_campaign", RANGE_FROM, YESTERDAY),
    ),

    "Campaign Wise Funnel Report — MTD": (
        _LEAD_FUNNEL,
        _paged_params("leadfunnel_campaign", MTD_FROM, YESTERDAY),
    ),

    "Appointment Status Report L1 Wise — MTD": (
        _APPOINTMENT,
        _paged_params("appointment_status", MTD_FROM, YESTERDAY),
    ),

}

# ─── HELPERS ─────────────────────────────────────────────────────────────────
def fetch(name: str, url: str, params: dict):
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"  URL    : {url}")
    print(f"  Params : {params}")
    print(f"{'='*60}")
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        print(f"  [HTTP ERROR] {e}")
    except requests.exceptions.ConnectionError:
        print(f"  [CONNECTION ERROR] Could not reach {url}")
    except requests.exceptions.Timeout:
        print(f"  [TIMEOUT] Request timed out")
    except Exception as e:
        print(f"  [ERROR] {e}")
    return None


def pretty_print(data):
    if data is None:
        print("  No data returned.")
        return
    print(json.dumps(data, indent=2, ensure_ascii=False))


# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    print(f"\nBhugoal Reports Automation")
    print(f"Yesterday    : {YESTERDAY}")
    print(f"Last 7 From  : {LAST_7_FROM}")
    print(f"MTD From     : {MTD_FROM}")
    print(f"Range From   : {RANGE_FROM}  (set via BHUGOAL_RANGE_FROM_DATE in .env)")
    print(f"Total Reports: {len(ENDPOINTS)}")

    all_data = {}
    for name, (url, params) in ENDPOINTS.items():
        data = fetch(name, url, params)
        all_data[name] = data
        pretty_print(data)

    print(f"\nDone. Fetched {len(all_data)} reports.")


if __name__ == "__main__":
    main()
