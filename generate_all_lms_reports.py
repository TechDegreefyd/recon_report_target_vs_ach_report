#!/usr/bin/env python3
"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
UNIFIED DAILY LMS REPORTS — Online + Regular
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Single script that generates BOTH:
  1. Daily Online LMS Report  (HTML)
  2. Daily Regular LMS Report (HTML)
And delivers both HTML files to the WhatsApp Admin Group.

Usage:  python3 generate_all_lms_reports.py
Environment:  .env at WORKSPACE_DIR (defaults to script directory)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import os
import sys
import json
import math
import calendar
import base64
import time
import shutil
import html
import re

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# ─── ARGS ───────────────────────────────────────────────────────────────────────
# --local   →  skip WhatsApp, keep files in OUTPUT_DIR instead of sending
# --plain   →  revert to plain (non-colourful) theme
LOCAL_MODE          = '--local'              in sys.argv
NO_CLEANUP          = '--nocleanup'          in sys.argv
COLORFUL_MODE       = '--plain'              not in sys.argv   # colourful is default
SIDEBAR_NAV         = '--no-sidebar'         not in sys.argv   # sidebar is default; pass --no-sidebar to revert
YESTERDAY           = '--yesterday'          in sys.argv       # force report_date = yesterday (IST); useful for 10 AM cron
LAST_ACTIVITY_ONLY  = '--last-activity-only' in sys.argv       # only generate + send the Last Activity tab (skips Regular LMS)
SKIP_LAST_ACTIVITY  = '--skip-last-activity' in sys.argv       # full run but skip sending Last Activity (sent separately at 10 AM)

import pandas as pd
import asyncpg
import requests
from datetime import datetime, timedelta, UTC
from dotenv import load_dotenv

from sheets_config import load_online_config, load_regular_config, log_report, get_service, sync_online_counsellors, sync_regular_dates, _norm

# ─── PATHS ──────────────────────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.environ.get('WORKSPACE_DIR', _SCRIPT_DIR)
load_dotenv(os.path.join(BASE_DIR, '.env'))

OUTPUT_DIR = os.path.join(BASE_DIR, 'Automation Cron Job', 'Target Report')
LOCAL_FALLBACK_DIR = os.path.join(BASE_DIR, 'Automation Cron Job', 'Target Report', 'local_fallback')
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(LOCAL_FALLBACK_DIR, exist_ok=True)

# ─── WHAPI ──────────────────────────────────────────────────────────────────────
WHAPI_TOKEN = os.getenv('WHAPI_TOKEN_PAID')

# ── Named groups — IDs are read from .env (comma-separated = multiple groups, auto-picked up)
_fallback = os.getenv('WHATSAPP_GROUP', '120363426619711887@g.us')
def _gids(env_key): return [g.strip() for g in os.getenv(env_key, _fallback).split(',') if g.strip()]

GROUPS = {
    # Add/remove IDs in .env under the matching key — no code change needed
    'Online LOB Reports':    _gids('WHATSAPP_GROUP_ONLINE_LOB'),  # Online LOB Reports only (MoM etc.)
    'Online Admission Team': _gids('WHATSAPP_GROUP_ONLINE_LMS'),# Online LOB Reports + Online Admission Team
    'Leadership_Regular':    _gids('WHATSAPP_GROUP_REGULAR_LMS'),
    'Daily Updates':         _gids('WHATSAPP_GROUP_DAILY_UPDATES'),
    # All Reports — receives every report regardless of caption mapping below
    # Set WHATSAPP_GROUP_ALL_REPORTS in .env to enable; leave unset to skip
    'All Reports':           _gids('WHATSAPP_GROUP_ALL_REPORTS') if os.getenv('WHATSAPP_GROUP_ALL_REPORTS') else [],
}

# ── Caption → Group name — edit here to control which group each report goes to
# Every report also goes to 'All Reports' group automatically (if configured above)
CAPTION_GROUP_MAP = {
    'Owner wise Achievement Report - Online Business':       'Online LOB Reports',
    'Online LOB - University wise Forms & Adm':              'Online LOB Reports',
    'Counsellor Targets vs Achievements — Fee & Admissions': 'Online Admission Team',
    'Admission and Application Ageing Report':               ['Online Admission Team', 'Online LOB Reports'],
    'Till Date Supervisor Month on Month':                   'Online LOB Reports',
    'Till Date University Month on Month':                   'Online LOB Reports',
    'Admission Target vs Achieved':                          'Leadership_Regular',
    'Form Target vs Achieved':                               'Leadership_Regular',
    'College Wise Monthly Comparison':                       'Leadership_Regular',
    'College ':                                               'Leadership_Regular',
    'Amity Total Forms - Campus YoY':                        'Daily Updates',
    'Amity Admissions - Campus YoY':                         'Daily Updates',
}

# ─── DATE LOGIC (IST, before 6AM = previous day) ────────────────────────────────
# Override with REPORT_DATE=YYYY-MM-DD env var to run for a specific date
if os.getenv('REPORT_DATE'):
    report_date = datetime.strptime(os.getenv('REPORT_DATE'), '%Y-%m-%d')
else:
    now_utc = datetime.now(UTC)
    now_ist = now_utc + timedelta(hours=5, minutes=30)
    if YESTERDAY or now_ist.hour < 6:
        report_date = now_ist - timedelta(days=1)
    else:
        report_date = now_ist

FTD_DATE = report_date.strftime('%Y-%m-%d')
MTD_START = report_date.replace(day=1).strftime('%Y-%m-%d')
MTD_END = FTD_DATE
MONTH_LABEL = report_date.strftime('%B %Y')
MONTH_SHORT = report_date.strftime('%b')

# Last month date range — same day window as current MTD
# e.g. today = Jun 9  →  compare May 1–9 vs Jun 1–9
_lm_first       = (report_date.replace(day=1) - timedelta(days=1)).replace(day=1)
_lm_same_day    = _lm_first.replace(day=min(report_date.day, calendar.monthrange(_lm_first.year, _lm_first.month)[1]))
LAST_MTD_START  = _lm_first.strftime('%Y-%m-%d')
LAST_MTD_END    = _lm_same_day.strftime('%Y-%m-%d')
LAST_MONTH_LABEL = _lm_first.strftime('%B %Y')

# N-day comparisons — N grows daily with report_date.day, two variants:
#   A) First N days of prev month  vs  First N days of current month  (same-window MoM)
#   B) Last N days of prev month   vs  First N days of current month  (momentum/transition)
_prev_last       = report_date.replace(day=1) - timedelta(days=1)
_days_in_lm      = calendar.monthrange(_lm_first.year, _lm_first.month)[1]
_n_days          = report_date.day   # grows every day, no cap
# Shared — first N days of current month (right side of both comparisons)
FIRST12_START    = report_date.replace(day=1).strftime('%Y-%m-%d')
FIRST12_END      = report_date.strftime('%Y-%m-%d')   # = FTD_DATE
# Comparison A — first N days of previous month (capped at prev month length)
LM_FIRST_N_START = _lm_first.strftime('%Y-%m-%d')
LM_FIRST_N_END   = _lm_first.replace(day=min(_n_days, _days_in_lm)).strftime('%Y-%m-%d')
# Comparison B — last N days of previous month (capped at prev month length)
_b_n             = min(_n_days, _days_in_lm)
LAST12_START     = (_prev_last - timedelta(days=_b_n - 1)).strftime('%Y-%m-%d')
LAST12_END       = _prev_last.strftime('%Y-%m-%d')
PREV_MONTH_LABEL = _prev_last.strftime('%B %Y')

# Timestamp of this run (IST) — appended to all output filenames
_run_ist = datetime.now(UTC) + timedelta(hours=5, minutes=30)
RUN_STAMP = _run_ist.strftime('%Y-%m-%d_%H-%M')

print(f"📅 Report Date (FTD): {FTD_DATE}")
print(f"📅 MTD: {MTD_START} → {MTD_END}")


# ═══════════════════════════════════════════════════════════════════════════════
# PART 1 — ONLINE LMS REPORT
# ═══════════════════════════════════════════════════════════════════════════════

# ─── Config (loaded from Google Sheets) ─────────────────────────────────────────
print("🔗 Loading config from Google Sheets...")
online_config = load_online_config()

# Online achievement is always MTD — no week window needed for Online
ONLINE_SUPERVISOR_TARGETS     = online_config.get("supervisor_targets", {})
ONLINE_COUNSELLOR_FEE_TARGETS = online_config.get("counsellor_fee_targets", {})   # flat {name: target}
ONLINE_COUNSELLOR_SUP_MAP     = online_config.get("counsellor_supervisor_map", {}) # {couns: sup} from targets tab

# ─── DB Connection ──────────────────────────────────────────────────────────────
ONLINE_DB = {
    "host": os.getenv("ONLINE_LMS_DB_HOST"),
    "port": int(os.getenv("ONLINE_LMS_DB_PORT", "54321")),
    "database": os.getenv("ONLINE_LMS_DB_NAME"),
    "user": os.getenv("ONLINE_LMS_DB_USER"),
    "password": os.getenv("ONLINE_LMS_DB_PASSWORD"),
}


async def online_get_data():
    conn = await asyncpg.connect(**ONLINE_DB)

    # ── Build counsellor roster directly from DB ─────────────────────────────
    # Only keep counsellors whose supervisor is tracked in Online_Targets sheet.
    tracked_supervisors = set(ONLINE_SUPERVISOR_TARGETS.keys())  # already _norm'd

    roster_rows = await conn.fetch("""
        SELECT c.counsellor_name, s.counsellor_name AS supervisor_name
        FROM counsellors c
        JOIN counsellors s ON s.counsellor_id = c.assigned_to
        WHERE c.status = 'active'
          AND c.assigned_to IS NOT NULL
          AND c.assigned_to != '[default]'
    """)

    # Supervisor aliases: targets tab uses short names, Online_Targets uses full names
    _SUP_ALIAS = {
        _norm('Vartika'): _norm('Vartika Thakur'),
        _norm('Vishal'):  _norm('Vishal Gaur'),
        _norm('Sid'):     _norm('Siddarth Kumar'),
    }

    # Only keep counsellors who have a real numeric target (excludes Anshika Dwivedi etc.)
    has_target = {c for c, t in ONLINE_COUNSELLOR_FEE_TARGETS.items() if t is not None}

    couns_data = []
    for r in roster_rows:
        sup   = _norm(r['supervisor_name'])
        couns = _norm(r['counsellor_name'])
        sup   = _SUP_ALIAS.get(sup, sup)
        if sup in tracked_supervisors and sup != couns and couns in has_target:
            couns_data.append({'supervisor_name': sup, 'counsellor_name': couns})

    df_couns = pd.DataFrame(couns_data).drop_duplicates(subset=['supervisor_name', 'counsellor_name']) \
               if couns_data else pd.DataFrame(columns=['supervisor_name', 'counsellor_name'])

    # Broader roster for Last Activity — tracked supervisor filter only, no has_target filter
    couns_data_all = []
    for r in roster_rows:
        sup   = _norm(r['supervisor_name'])
        couns = _norm(r['counsellor_name'])
        sup   = _SUP_ALIAS.get(sup, sup)
        if sup in tracked_supervisors and sup != couns:
            couns_data_all.append({'supervisor_name': sup, 'counsellor_name': couns})
    df_couns_all = pd.DataFrame(couns_data_all).drop_duplicates(subset=['supervisor_name', 'counsellor_name']) \
                   if couns_data_all else pd.DataFrame(columns=['supervisor_name', 'counsellor_name'])

    # Supplement with counsellors that have a target in the sheet but are missing from DB
    db_couns_set     = set(df_couns['counsellor_name'].tolist())
    db_couns_all_set = set(df_couns_all['counsellor_name'].tolist())
    extra = []
    extra_all = []
    for couns, sup in ONLINE_COUNSELLOR_SUP_MAP.items():
        sup = _SUP_ALIAS.get(sup, sup)
        if sup not in tracked_supervisors:
            continue
        if couns not in db_couns_set and couns in has_target:
            extra.append({'supervisor_name': sup, 'counsellor_name': couns})
        if couns not in db_couns_all_set:
            extra_all.append({'supervisor_name': sup, 'counsellor_name': couns})
    if extra:
        df_couns = pd.concat([df_couns, pd.DataFrame(extra)], ignore_index=True)
    if extra_all:
        df_couns_all = pd.concat([df_couns_all, pd.DataFrame(extra_all)], ignore_index=True)

    YTD_START  = f'{report_date.year}-01-01'
    MTD_START_ = MTD_START
    MTD_END_   = MTD_END
    FTD_DATE_  = FTD_DATE

    # ── Counsellor MTD fee: dedup per student (keep highest deposit), sum per counsellor ──
    couns_mtd_fee_query = f"""
    WITH deduped AS (
        SELECT DISTINCT ON (csj.student_id)
            s.assigned_counsellor_id,
            csj.deposit_amount
        FROM course_status_journeys csj
        JOIN students s ON s.student_id = csj.student_id
        WHERE csj.course_status = 'Admission'
          AND INITCAP(TRIM(csj.fee_type)) NOT IN ('Partial Paid', 'Partially Paid', 'Partial Done')
          AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date >= '{MTD_START_}'::date
          AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date <= '{MTD_END_}'::date
        ORDER BY csj.student_id, csj.deposit_amount DESC
    )
    SELECT c.counsellor_name, COALESCE(SUM(d.deposit_amount), 0) AS mtd_fee
    FROM deduped d
    LEFT JOIN counsellors c ON c.counsellor_id = d.assigned_counsellor_id
    GROUP BY c.counsellor_name;
    """

    # ── Counsellor FTD fee: same logic, single day ──
    couns_ftd_fee_query = f"""
    WITH deduped AS (
        SELECT DISTINCT ON (csj.student_id)
            s.assigned_counsellor_id,
            csj.deposit_amount
        FROM course_status_journeys csj
        JOIN students s ON s.student_id = csj.student_id
        WHERE csj.course_status = 'Admission'
          AND INITCAP(TRIM(csj.fee_type)) NOT IN ('Partial Paid', 'Partially Paid', 'Partial Done')
          AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date = '{FTD_DATE_}'::date
        ORDER BY csj.student_id, csj.deposit_amount DESC
    )
    SELECT c.counsellor_name, COALESCE(SUM(d.deposit_amount), 0) AS ftd_fee
    FROM deduped d
    LEFT JOIN counsellors c ON c.counsellor_id = d.assigned_counsellor_id
    GROUP BY c.counsellor_name;
    """

    # ── Counsellor MTD admissions: distinct students per counsellor ──
    couns_mtd_adm_query = f"""
    SELECT c.counsellor_name, COUNT(DISTINCT csj.student_id) AS mtd_adm
    FROM course_status_journeys csj
    JOIN students s ON s.student_id = csj.student_id
    LEFT JOIN counsellors c ON c.counsellor_id = s.assigned_counsellor_id
    WHERE csj.course_status = 'Admission'
      AND INITCAP(TRIM(csj.fee_type)) NOT IN ('Partial Paid', 'Partially Paid', 'Partial Done')
      AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date >= '{MTD_START_}'::date
      AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date <= '{MTD_END_}'::date
    GROUP BY c.counsellor_name;
    """

    # ── Counsellor FTD admissions ──
    couns_ftd_adm_query = f"""
    SELECT c.counsellor_name, COUNT(DISTINCT csj.student_id) AS ftd_adm
    FROM course_status_journeys csj
    JOIN students s ON s.student_id = csj.student_id
    LEFT JOIN counsellors c ON c.counsellor_id = s.assigned_counsellor_id
    WHERE csj.course_status = 'Admission'
      AND INITCAP(TRIM(csj.fee_type)) NOT IN ('Partial Paid', 'Partially Paid', 'Partial Done')
      AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date = '{FTD_DATE_}'::date
    GROUP BY c.counsellor_name;
    """

    # ── College performance: YTD + MTD + FTD forms and admissions, all from DB ──
    college_query = f"""
    SELECT
        uc.university_name AS college_name,
        COUNT(DISTINCT CASE
            WHEN csj.course_status = 'Application'
             AND csj.created_at >= '{YTD_START}'::timestamptz
             AND csj.created_at < CURRENT_DATE + INTERVAL '1 day' - INTERVAL '5 hours 30 minutes'
            THEN csj.student_id || '_' || csj.course_id END) AS ytd_forms,
        COUNT(DISTINCT CASE
            WHEN csj.course_status IN ('Admission', 'Enrolled')
             AND INITCAP(TRIM(csj.fee_type)) NOT IN ('Partial Paid', 'Partially Paid', 'Partial Done')
             AND csj.created_at >= '{YTD_START}'::timestamptz
             AND csj.created_at < CURRENT_DATE + INTERVAL '1 day' - INTERVAL '5 hours 30 minutes'
            THEN csj.student_id END) AS ytd_adm,
        COUNT(DISTINCT CASE
            WHEN csj.course_status = 'Application'
             AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date >= '{MTD_START_}'::date
             AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date <= '{MTD_END_}'::date
            THEN csj.student_id || '_' || csj.course_id END) AS mtd_forms,
        COUNT(DISTINCT CASE
            WHEN csj.course_status IN ('Admission', 'Enrolled')
             AND INITCAP(TRIM(csj.fee_type)) NOT IN ('Partial Paid', 'Partially Paid', 'Partial Done')
             AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date >= '{MTD_START_}'::date
             AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date <= '{MTD_END_}'::date
            THEN csj.student_id END) AS mtd_adm,
        COUNT(DISTINCT CASE
            WHEN csj.course_status = 'Application'
             AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date = '{FTD_DATE_}'::date
            THEN csj.student_id || '_' || csj.course_id END) AS ftd_forms,
        COUNT(DISTINCT CASE
            WHEN csj.course_status IN ('Admission', 'Enrolled')
             AND INITCAP(TRIM(csj.fee_type)) NOT IN ('Partial Paid', 'Partially Paid', 'Partial Done')
             AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date = '{FTD_DATE_}'::date
            THEN csj.student_id END) AS ftd_adm
    FROM course_status_journeys csj
    JOIN university_courses uc ON uc.course_id = csj.course_id
    WHERE csj.created_at >= '{YTD_START}'::timestamptz
      AND csj.created_at < CURRENT_DATE + INTERVAL '1 day' - INTERVAL '5 hours 30 minutes'
    GROUP BY uc.university_name
    HAVING COUNT(DISTINCT CASE
        WHEN csj.course_status IN ('Application', 'Admission', 'Enrolled')
        THEN csj.student_id END) > 0;
    """

    last_activity_query = """
    WITH last_adm AS (
      SELECT DISTINCT ON (attr_counsellor_id)
        attr_counsellor_id AS counsellor_id,
        MIN(created_at) AT TIME ZONE 'Asia/Kolkata' AS last_admission_date
      FROM (
        SELECT csj.created_at,
          CASE WHEN mc.role = 'to' THEN s.assigned_counsellor_id
               ELSE csj.counsellor_id
          END AS attr_counsellor_id,
          csj.student_id, csj.course_id
        FROM course_status_journeys csj
        JOIN students s ON s.student_id = csj.student_id
        JOIN counsellors mc ON mc.counsellor_id = csj.counsellor_id
        WHERE csj.course_status = 'Admission'
          AND INITCAP(TRIM(csj.fee_type)) NOT IN ('Partial Paid','Partially Paid','Partial Done')
      ) x
      GROUP BY attr_counsellor_id, student_id, course_id
      ORDER BY attr_counsellor_id, MIN(created_at) DESC
    ),
    last_adm_final AS (
      SELECT counsellor_id, MAX(last_admission_date) AS last_admission
      FROM last_adm GROUP BY counsellor_id
    ),
    last_app AS (
      SELECT DISTINCT ON (attr_counsellor_id)
        attr_counsellor_id AS counsellor_id,
        MIN(created_at) AT TIME ZONE 'Asia/Kolkata' AS last_app_date
      FROM (
        SELECT csj.created_at,
          CASE WHEN mc.role = 'to' THEN s.assigned_counsellor_id
               ELSE csj.counsellor_id
          END AS attr_counsellor_id,
          csj.student_id, csj.course_id
        FROM course_status_journeys csj
        JOIN students s ON s.student_id = csj.student_id
        JOIN counsellors mc ON mc.counsellor_id = csj.counsellor_id
        WHERE csj.course_status = 'Application'
      ) x
      GROUP BY attr_counsellor_id, student_id, course_id
      ORDER BY attr_counsellor_id, MIN(created_at) DESC
    ),
    last_app_final AS (
      SELECT counsellor_id, MAX(last_app_date) AS last_application
      FROM last_app GROUP BY counsellor_id
    )
    SELECT
      c.counsellor_name,
      m.counsellor_name AS supervisor,
      la.last_admission::DATE AS last_admission,
      lap.last_application::DATE AS last_application
    FROM counsellors c
    JOIN counsellors m ON c.assigned_to = m.counsellor_id
    LEFT JOIN last_adm_final la ON c.counsellor_id = la.counsellor_id
    LEFT JOIN last_app_final lap ON c.counsellor_id = lap.counsellor_id
    WHERE c.role = 'l2'
    ORDER BY m.counsellor_name, c.counsellor_name;
    """

    # ── Last month counsellor admissions (for MoM supervisor comparison) ──
    couns_last_mtd_adm_query = f"""
    SELECT c.counsellor_name, COUNT(DISTINCT csj.student_id) AS last_mtd_adm
    FROM course_status_journeys csj
    JOIN students s ON s.student_id = csj.student_id
    LEFT JOIN counsellors c ON c.counsellor_id = s.assigned_counsellor_id
    WHERE csj.course_status = 'Admission'
      AND INITCAP(TRIM(csj.fee_type)) NOT IN ('Partial Paid', 'Partially Paid', 'Partial Done')
      AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date >= '{LAST_MTD_START}'::date
      AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date <= '{LAST_MTD_END}'::date
    GROUP BY c.counsellor_name;
    """

    # ── Last month college admissions (for MoM university comparison) ──
    college_last_mtd_query = f"""
    SELECT
        uc.university_name AS college_name,
        COUNT(DISTINCT CASE
            WHEN csj.course_status IN ('Admission', 'Enrolled')
             AND INITCAP(TRIM(csj.fee_type)) NOT IN ('Partial Paid', 'Partially Paid', 'Partial Done')
             AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date >= '{LAST_MTD_START}'::date
             AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date <= '{LAST_MTD_END}'::date
            THEN csj.student_id END) AS last_mtd_adm
    FROM course_status_journeys csj
    JOIN university_courses uc ON uc.course_id = csj.course_id
    WHERE (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date >= '{LAST_MTD_START}'::date
      AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date <= '{LAST_MTD_END}'::date
    GROUP BY uc.university_name;
    """

    # ── This month college fee collected (for MoM university fee comparison) ──
    college_mtd_fee_query = f"""
    WITH deduped AS (
        SELECT DISTINCT ON (csj.student_id, uc.university_name)
            uc.university_name AS college_name,
            csj.deposit_amount
        FROM course_status_journeys csj
        JOIN university_courses uc ON uc.course_id = csj.course_id
        WHERE csj.course_status = 'Admission'
          AND INITCAP(TRIM(csj.fee_type)) NOT IN ('Partial Paid', 'Partially Paid', 'Partial Done')
          AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date >= '{MTD_START_}'::date
          AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date <= '{MTD_END_}'::date
        ORDER BY csj.student_id, uc.university_name, csj.deposit_amount DESC
    )
    SELECT college_name, COALESCE(SUM(deposit_amount), 0) AS mtd_fee
    FROM deduped GROUP BY college_name;
    """

    # ── Last month college fee collected (for MoM university fee comparison) ──
    college_last_mtd_fee_query = f"""
    WITH deduped AS (
        SELECT DISTINCT ON (csj.student_id, uc.university_name)
            uc.university_name AS college_name,
            csj.deposit_amount
        FROM course_status_journeys csj
        JOIN university_courses uc ON uc.course_id = csj.course_id
        WHERE csj.course_status = 'Admission'
          AND INITCAP(TRIM(csj.fee_type)) NOT IN ('Partial Paid', 'Partially Paid', 'Partial Done')
          AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date >= '{LAST_MTD_START}'::date
          AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date <= '{LAST_MTD_END}'::date
        ORDER BY csj.student_id, uc.university_name, csj.deposit_amount DESC
    )
    SELECT college_name, COALESCE(SUM(deposit_amount), 0) AS last_mtd_fee
    FROM deduped GROUP BY college_name;
    """

    # ── Last month counsellor fee collected (for MoM revenue comparison) ──
    couns_last_mtd_fee_query = f"""
    WITH deduped AS (
        SELECT DISTINCT ON (csj.student_id)
            s.assigned_counsellor_id,
            csj.deposit_amount
        FROM course_status_journeys csj
        JOIN students s ON s.student_id = csj.student_id
        WHERE csj.course_status = 'Admission'
          AND INITCAP(TRIM(csj.fee_type)) NOT IN ('Partial Paid', 'Partially Paid', 'Partial Done')
          AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date >= '{LAST_MTD_START}'::date
          AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date <= '{LAST_MTD_END}'::date
        ORDER BY csj.student_id, csj.deposit_amount DESC
    )
    SELECT c.counsellor_name, COALESCE(SUM(d.deposit_amount), 0) AS last_mtd_fee
    FROM deduped d
    LEFT JOIN counsellors c ON c.counsellor_id = d.assigned_counsellor_id
    GROUP BY c.counsellor_name;
    """

    rows_mtd_fee              = await conn.fetch(couns_mtd_fee_query)
    rows_ftd_fee              = await conn.fetch(couns_ftd_fee_query)
    rows_mtd_adm              = await conn.fetch(couns_mtd_adm_query)
    rows_ftd_adm              = await conn.fetch(couns_ftd_adm_query)
    rows_college              = await conn.fetch(college_query)
    rows_last_activity        = await conn.fetch(last_activity_query)
    rows_last_mtd_adm         = await conn.fetch(couns_last_mtd_adm_query)
    rows_college_last_mtd     = await conn.fetch(college_last_mtd_query)
    rows_last_mtd_fee         = await conn.fetch(couns_last_mtd_fee_query)
    rows_college_mtd_fee      = await conn.fetch(college_mtd_fee_query)
    rows_college_last_mtd_fee = await conn.fetch(college_last_mtd_fee_query)
    await conn.close()

    def _norm_name(df, col):
        if col not in df.columns:
            df[col] = pd.Series(dtype=str)
        df[col] = df[col].apply(_norm)
        return df

    def _safe_df(rows, columns):
        if rows:
            return pd.DataFrame([dict(r) for r in rows])
        return pd.DataFrame(columns=columns)

    df_couns_fee = _norm_name(_safe_df(rows_mtd_fee, ['counsellor_name', 'mtd_fee']), 'counsellor_name') \
        .merge(_norm_name(_safe_df(rows_ftd_fee, ['counsellor_name', 'ftd_fee']), 'counsellor_name'),
               on='counsellor_name', how='outer').fillna(0)

    df_couns_adm = _norm_name(_safe_df(rows_mtd_adm, ['counsellor_name', 'mtd_adm']), 'counsellor_name') \
        .merge(_norm_name(_safe_df(rows_ftd_adm, ['counsellor_name', 'ftd_adm']), 'counsellor_name'),
               on='counsellor_name', how='outer').fillna(0)

    df_college = pd.DataFrame([dict(r) for r in rows_college])

    df_last_activity_raw = _norm_name(
        _safe_df(rows_last_activity, ['counsellor_name', 'supervisor', 'last_admission', 'last_application']),
        'counsellor_name'
    )
    df_last_activity_raw = _norm_name(df_last_activity_raw, 'supervisor')

    df_last_mtd_adm         = _norm_name(_safe_df(rows_last_mtd_adm, ['counsellor_name', 'last_mtd_adm']), 'counsellor_name')
    df_college_last_mtd     = _safe_df(rows_college_last_mtd, ['college_name', 'last_mtd_adm'])
    df_last_mtd_fee         = _norm_name(_safe_df(rows_last_mtd_fee, ['counsellor_name', 'last_mtd_fee']), 'counsellor_name')
    df_college_mtd_fee      = _safe_df(rows_college_mtd_fee, ['college_name', 'mtd_fee'])
    df_college_last_mtd_fee = _safe_df(rows_college_last_mtd_fee, ['college_name', 'last_mtd_fee'])

    return df_couns, df_couns_fee, df_couns_adm, df_college, df_last_activity_raw, df_couns_all, df_last_mtd_adm, df_college_last_mtd, df_last_mtd_fee, df_college_mtd_fee, df_college_last_mtd_fee


def online_pct(achieved, target):
    achieved = 0 if pd.isna(achieved) else achieved
    if target is None:
        return "—"
    target = 0 if pd.isna(target) else target
    if isinstance(achieved, float) and achieved.is_integer():
        achieved = int(achieved)
    if isinstance(target, float) and target.is_integer():
        target = int(target)
    if target == 0:
        return "0.0%"
    return f"{(achieved / target * 100):.1f}%"


def online_prepare_data(df_couns, df_couns_fee, df_couns_adm, df_college, df_last_activity_raw=None, df_couns_all=None, df_last_mtd_adm=None, df_college_last_mtd=None, df_last_mtd_fee=None, df_college_mtd_fee=None, df_college_last_mtd_fee=None):
    """Build all Online report DataFrames from pre-aggregated SQL results."""

    sup_order     = list(ONLINE_SUPERVISOR_TARGETS.keys())
    display_names = {s: s for s in sup_order}

    # Merge SQL aggregates onto counsellor roster
    df_c_rev = df_couns \
        .merge(df_couns_fee.rename(columns={'mtd_fee': 'Achieved', 'ftd_fee': 'FTD'}),
               on='counsellor_name', how='left').fillna(0)
    df_c_rev['Target'] = df_c_rev['counsellor_name'].map(
        lambda n: ONLINE_COUNSELLOR_FEE_TARGETS.get(n, 0))
    # None means "no target set" (displayed as '—'); treat as 0 only for supervisor roll-up sums
    df_c_rev['Ach %'] = df_c_rev.apply(lambda r: online_pct(r['Achieved'], r['Target']), axis=1)
    df_c_rev['_target_numeric'] = df_c_rev['Target'].apply(lambda v: 0 if v is None else v)

    df_c_adm = df_couns \
        .merge(df_couns_adm.rename(columns={'mtd_adm': 'Achieve', 'ftd_adm': 'FTD'}),
               on='counsellor_name', how='left').fillna(0)

    # ── Counsellor_Fee_Collected ─────────────────────────────────────────────
    rows = []
    for sup in sup_order:
        s = df_c_rev[df_c_rev['supervisor_name'] == sup]
        if s.empty:
            continue
        for _, r in s.iterrows():
            rows.append([display_names.get(sup, sup), r['counsellor_name'], r['Target'], r['Achieved'], r['Ach %'], r['FTD']])
        sup_tgt_sum = s['_target_numeric'].sum()
        rows.append([f'Total ({display_names.get(sup, sup)})', '', sup_tgt_sum, s['Achieved'].sum(),
                     online_pct(s['Achieved'].sum(), sup_tgt_sum), s['FTD'].sum()])
    gt_tgt_num = df_c_rev['_target_numeric'].sum()
    rows.append(['Grand Total', '', gt_tgt_num, df_c_rev['Achieved'].sum(),
                 online_pct(df_c_rev['Achieved'].sum(), gt_tgt_num), df_c_rev['FTD'].sum()])
    df_c_rev_sheet = pd.DataFrame(rows, columns=['Supervisor', 'Counsellor', 'Target', 'Fee Collected', 'Ach %', 'FTD'])

    # ── Supervisor_Fee_Collected ─────────────────────────────────────────────
    rows = []
    for sup in sup_order:
        s = df_c_rev[df_c_rev['supervisor_name'] == sup]
        sup_tgt = ONLINE_SUPERVISOR_TARGETS.get(sup, 0)
        rows.append([display_names.get(sup, sup), sup_tgt, s['Achieved'].sum(), online_pct(s['Achieved'].sum(), sup_tgt), s['FTD'].sum()])
    gt_tgt = sum(ONLINE_SUPERVISOR_TARGETS.values())
    gt_ach = df_c_rev['Achieved'].sum()
    gt_ftd = df_c_rev['FTD'].sum()
    rows.append(['Grand Total', gt_tgt, gt_ach, online_pct(gt_ach, gt_tgt), gt_ftd])
    df_sup_rev_sheet = pd.DataFrame(rows, columns=['Supervisor', 'Target', 'Fee Collected', 'Ach %', 'FTD'])

    # ── Counsellor_Admission ─────────────────────────────────────────────────
    rows = []
    for sup in sup_order:
        s = df_c_adm[df_c_adm['supervisor_name'] == sup]
        if s.empty:
            continue
        for _, r in s.iterrows():
            rows.append([display_names.get(sup, sup), r['counsellor_name'], r['Achieve'], r['FTD']])
        rows.append([f'Total ({display_names.get(sup, sup)})', '', s['Achieve'].sum(), s['FTD'].sum()])
    rows.append(['Grand Total', '', df_c_adm['Achieve'].sum(), df_c_adm['FTD'].sum()])
    df_c_adm_sheet = pd.DataFrame(rows, columns=['Supervisor', 'Counsellor', 'Achieve', 'FTD'])

    # ── Supervisor_Admission ─────────────────────────────────────────────────
    rows = []
    for sup in sup_order:
        s = df_c_adm[df_c_adm['supervisor_name'] == sup]
        rows.append([display_names.get(sup, sup), s['Achieve'].sum(), s['FTD'].sum()])
    rows.append(['Grand Total', df_c_adm['Achieve'].sum(), df_c_adm['FTD'].sum()])
    df_sup_adm_sheet = pd.DataFrame(rows, columns=['Supervisor', 'Achieve', 'FTD'])

    # ── College_Performance ──────────────────────────────────────────────────
    df_col = df_college.fillna(0).sort_values('ytd_forms', ascending=False)
    rows = []
    for _, r in df_col.iterrows():
        c_name = str(r['college_name']).title()
        for wrong, right in [('Gla', 'GLA'), ('Lpu', 'LPU'), ('Iit', 'IIT'), ('Nit', 'NIT'), ('Smu', 'SMU'), ('Nmims', 'NMIMS')]:
            c_name = c_name.replace(wrong, right)
        rows.append([c_name,
                     int(r['ytd_forms']), int(r['ytd_adm']), online_pct(r['ytd_adm'], r['ytd_forms']),
                     int(r['mtd_forms']), int(r['mtd_adm']), online_pct(r['mtd_adm'], r['mtd_forms']),
                     int(r['ftd_forms']), int(r['ftd_adm']), online_pct(r['ftd_adm'], r['ftd_forms'])])
    rows.append(['Total',
                 int(df_col['ytd_forms'].sum()), int(df_col['ytd_adm'].sum()),
                 online_pct(df_col['ytd_adm'].sum(), df_col['ytd_forms'].sum()),
                 int(df_col['mtd_forms'].sum()), int(df_col['mtd_adm'].sum()),
                 online_pct(df_col['mtd_adm'].sum(), df_col['mtd_forms'].sum()),
                 int(df_col['ftd_forms'].sum()), int(df_col['ftd_adm'].sum()),
                 online_pct(df_col['ftd_adm'].sum(), df_col['ftd_forms'].sum())])
    df_college_sheet = pd.DataFrame(rows, columns=['Colleges', 'YTD Forms', 'YTD Admissions', 'YTD F2A %',
                                                    'MTD Forms', 'MTD Admissions', 'MTD F2A %',
                                                    'FTD Forms', 'FTD Admissions', 'FTD F2A %'])

    # ── Counsellor_Last_Activity — roster from Google Sheet, dates from SQL ─────
    # df_couns_all controls WHO appears; df_last_activity_raw is a date lookup only
    _raw     = df_last_activity_raw if df_last_activity_raw is not None else pd.DataFrame(
        columns=['counsellor_name', 'last_admission', 'last_application'])
    # roster = everyone explicitly listed in the sheet (numeric target OR "-"), under a tracked supervisor
    _in_sheet = set(ONLINE_COUNSELLOR_FEE_TARGETS.keys())
    _roster   = (df_couns_all if df_couns_all is not None else df_couns)
    _roster   = _roster[_roster['counsellor_name'].isin(_in_sheet)]
    _today   = report_date.date() if hasattr(report_date, 'date') else report_date
    rows = []
    for sup in sup_order:
        team = _roster[_roster['supervisor_name'] == sup]
        if team.empty:
            continue
        for _, r in team.iterrows():
            cn      = r['counsellor_name']
            match   = _raw[_raw['counsellor_name'] == cn]
            # last admission
            la_date = match.iloc[0]['last_admission'] if not match.empty else None
            if la_date is not None and not (isinstance(la_date, float) and math.isnan(la_date)):
                if hasattr(la_date, 'date'): la_date = la_date.date()
                last_adm_str = la_date.strftime('%d %b %Y')
                days_adm     = (_today - la_date).days
            else:
                last_adm_str, days_adm = '—', None
            # last application
            lap_date = match.iloc[0]['last_application'] if not match.empty else None
            if lap_date is not None and not (isinstance(lap_date, float) and math.isnan(lap_date)):
                if hasattr(lap_date, 'date'): lap_date = lap_date.date()
                last_app_str = lap_date.strftime('%d %b %Y')
                days_app     = (_today - lap_date).days
            else:
                last_app_str, days_app = '—', None
            rows.append([display_names.get(sup, sup), cn, last_adm_str, days_adm, last_app_str, days_app])
    df_last_activity = pd.DataFrame(rows, columns=['Supervisor', 'Counsellor', 'Last Admission', 'Days Since Adm', 'Last Application', 'Days Since App'])

    # ── Month-over-Month lookups ──────────────────────────────────────────────
    _last_adm_lookup = {}
    if df_last_mtd_adm is not None and not df_last_mtd_adm.empty:
        _last_adm_lookup = dict(zip(df_last_mtd_adm['counsellor_name'], df_last_mtd_adm['last_mtd_adm']))
    _last_fee_lookup = {}
    if df_last_mtd_fee is not None and not df_last_mtd_fee.empty:
        _last_fee_lookup = dict(zip(df_last_mtd_fee['counsellor_name'], df_last_mtd_fee['last_mtd_fee']))

    def _chg_pct(chg, last):
        if last: return f"{(chg / last * 100):+.1f}%"
        return "+100%" if chg > 0 else "N/A"

    # ── Supervisor Month-over-Month Comparison ────────────────────────────────
    sup_mom_rows = []
    for sup in sup_order:
        s_adm  = df_c_adm[df_c_adm['supervisor_name'] == sup]
        s_rev  = df_c_rev[df_c_rev['supervisor_name'] == sup]
        this_adm = int(s_adm['Achieve'].sum()) if not s_adm.empty else 0
        this_fee = float(s_rev['Achieved'].sum()) if not s_rev.empty else 0.0
        last_adm = last_fee = 0
        for _, r in s_adm.iterrows():
            cn = r['counsellor_name']
            last_adm += int(_last_adm_lookup.get(cn, 0))
            last_fee += float(_last_fee_lookup.get(cn, 0))
        chg_adm = this_adm - last_adm
        chg_fee = this_fee - last_fee
        sup_mom_rows.append([display_names.get(sup, sup), last_adm, this_adm, chg_adm, _chg_pct(chg_adm, last_adm), last_fee, this_fee, chg_fee, _chg_pct(chg_fee, last_fee)])
    gt_la = sum(r[1] for r in sup_mom_rows); gt_ta = sum(r[2] for r in sup_mom_rows)
    gt_lf = sum(r[5] for r in sup_mom_rows); gt_tf = sum(r[6] for r in sup_mom_rows)
    sup_mom_rows.append(['Grand Total', gt_la, gt_ta, gt_ta - gt_la, _chg_pct(gt_ta - gt_la, gt_la), gt_lf, gt_tf, gt_tf - gt_lf, _chg_pct(gt_tf - gt_lf, gt_lf)])
    df_sup_mom = pd.DataFrame(sup_mom_rows, columns=['Supervisor', f'Adm {LAST_MONTH_LABEL}', f'Adm {MONTH_LABEL}', 'Adm Change', 'Adm %', f'Fee {LAST_MONTH_LABEL}', f'Fee {MONTH_LABEL}', 'Fee Change', 'Fee %'])

    # ── Counsellor Month-over-Month Comparison (adm + revenue) ───────────────
    couns_mom_rows = []
    for sup in sup_order:
        s_adm = df_c_adm[df_c_adm['supervisor_name'] == sup]
        s_rev = df_c_rev[df_c_rev['supervisor_name'] == sup]
        if s_adm.empty:
            continue
        for _, r in s_adm.iterrows():
            cn = r['counsellor_name']
            this_adm = int(float(r['Achieve']))
            last_adm = int(_last_adm_lookup.get(cn, 0))
            rev_row  = s_rev[s_rev['counsellor_name'] == cn]
            this_fee = float(rev_row.iloc[0]['Achieved']) if not rev_row.empty else 0.0
            last_fee = float(_last_fee_lookup.get(cn, 0))
            chg_adm  = this_adm - last_adm
            chg_fee  = this_fee - last_fee
            couns_mom_rows.append([display_names.get(sup, sup), cn, last_adm, this_adm, chg_adm, _chg_pct(chg_adm, last_adm), last_fee, this_fee, chg_fee, _chg_pct(chg_fee, last_fee)])
    df_couns_mom = pd.DataFrame(couns_mom_rows, columns=['Supervisor', 'Counsellor', f'Adm {LAST_MONTH_LABEL}', f'Adm {MONTH_LABEL}', 'Adm Change', 'Adm %', f'Fee {LAST_MONTH_LABEL}', f'Fee {MONTH_LABEL}', 'Fee Change', 'Fee %'])

    # ── College Month-over-Month Comparison ───────────────────────────────────
    _last_col_adm_lookup = {}
    if df_college_last_mtd is not None and not df_college_last_mtd.empty:
        _last_col_adm_lookup = dict(zip(df_college_last_mtd['college_name'], df_college_last_mtd['last_mtd_adm']))
    _this_col_fee_lookup = {}
    if df_college_mtd_fee is not None and not df_college_mtd_fee.empty:
        _this_col_fee_lookup = dict(zip(df_college_mtd_fee['college_name'], df_college_mtd_fee['mtd_fee']))
    _last_col_fee_lookup = {}
    if df_college_last_mtd_fee is not None and not df_college_last_mtd_fee.empty:
        _last_col_fee_lookup = dict(zip(df_college_last_mtd_fee['college_name'], df_college_last_mtd_fee['last_mtd_fee']))

    col_mom_rows = []
    for _, r in df_college.iterrows():
        cname    = r['college_name']
        this_adm = int(r.get('mtd_adm', 0))
        last_adm = int(_last_col_adm_lookup.get(cname, 0))
        this_fee = float(_this_col_fee_lookup.get(cname, 0))
        last_fee = float(_last_col_fee_lookup.get(cname, 0))
        chg_adm  = this_adm - last_adm
        chg_fee  = this_fee - last_fee
        adm_pct  = f"{(chg_adm / last_adm * 100):+.1f}%" if last_adm else ("+100%" if this_adm else "N/A")
        fee_pct  = f"{(chg_fee / last_fee * 100):+.1f}%" if last_fee else ("+100%" if this_fee else "N/A")
        c_name_disp = str(cname).title()
        for wrong, right in [('Gla', 'GLA'), ('Lpu', 'LPU'), ('Iit', 'IIT'), ('Nit', 'NIT'), ('Smu', 'SMU'), ('Nmims', 'NMIMS')]:
            c_name_disp = c_name_disp.replace(wrong, right)
        col_mom_rows.append([c_name_disp, last_adm, this_adm, chg_adm, adm_pct, last_fee, this_fee, chg_fee, fee_pct])
    col_mom_rows.sort(key=lambda x: -x[2])  # sort by this month admissions desc
    gt_col_la = sum(r[1] for r in col_mom_rows); gt_col_ta = sum(r[2] for r in col_mom_rows)
    gt_col_lf = sum(r[5] for r in col_mom_rows); gt_col_tf = sum(r[6] for r in col_mom_rows)
    gt_col_ca = gt_col_ta - gt_col_la; gt_col_cf = gt_col_tf - gt_col_lf
    col_mom_rows.append(['Total', gt_col_la, gt_col_ta, gt_col_ca,
                         f"{(gt_col_ca/gt_col_la*100):+.1f}%" if gt_col_la else "N/A",
                         gt_col_lf, gt_col_tf, gt_col_cf,
                         f"{(gt_col_cf/gt_col_lf*100):+.1f}%" if gt_col_lf else "N/A"])
    df_col_mom = pd.DataFrame(col_mom_rows, columns=[
        'College', f'Adm {LAST_MONTH_LABEL}', f'Adm {MONTH_LABEL}', 'Adm Change', 'Adm %',
        f'Fee {LAST_MONTH_LABEL}', f'Fee {MONTH_LABEL}', 'Fee Change', 'Fee %'])

    print("✅ Online LMS data prepared")
    return {
        'Counsellor_Fee_Collected':    df_c_rev_sheet,
        'Supervisor_Fee_Collected':    df_sup_rev_sheet,
        'Counsellor_Admission':        df_c_adm_sheet,
        'Supervisor_Admission':        df_sup_adm_sheet,
        'College_Performance':         df_college_sheet,
        'Counsellor_Last_Activity':    df_last_activity,
        'Supervisor_Month_Comparison': df_sup_mom,
        'Counsellor_Month_Comparison': df_couns_mom,
        'College_Month_Comparison':    df_col_mom,
    }


def online_generate_html(sheets):
    """Generate Online LMS HTML dashboard directly from in-memory DataFrames."""
    out_path = os.path.join(OUTPUT_DIR, f'Degreefyd_Online_LMS_HTML_Report_{RUN_STAMP}.html')

    adm_targets = online_config.get('supervisor_admission_targets', {})

    sup_rev = sheets['Supervisor_Fee_Collected']
    c_rev = sheets['Counsellor_Fee_Collected']
    sup_adm = sheets['Supervisor_Admission']
    c_adm = sheets['Counsellor_Admission']
    college = sheets['College_Performance']
    last_activity = sheets.get('Counsellor_Last_Activity', pd.DataFrame())
    sup_mom   = sheets.get('Supervisor_Month_Comparison', pd.DataFrame())
    couns_mom = sheets.get('Counsellor_Month_Comparison', pd.DataFrame())
    col_mom   = sheets.get('College_Month_Comparison', pd.DataFrame())

    def esc(v):
        return html.escape(str(v))

    def money(v):
        try:
            v = float(v)
        except:
            return '\u20b90'
        if v == 0:
            return '\u20b90'
        if abs(v) >= 100000:
            lakh_val = round(v / 100000, 1)
            formatted = f'{lakh_val:.1f}'.rstrip('0').rstrip('.')
            return f'\u20b9{formatted}L'
        return f'\u20b9{v:,.0f}'

    def money_full(v):
        if v is None:
            return '\u2014'
        try:
            v = float(v)
        except:
            return '\u2014'
        if not v:
            return '\u20b90'
        if abs(v) >= 100000:
            lakh_val = round(v / 100000, 1)
            formatted = f'{lakh_val:.1f}'.rstrip('0').rstrip('.')
            return f'\u20b9{formatted}L'
        return f'\u20b9{v:,.0f}'

    def num(v):
        try:
            if pd.isna(v) or float(v) == 0:
                return '\u2014'
            return f'{int(float(v)):,}'
        except:
            return esc(v)

    def pct_value(s):
        text = str(s)
        if text in ('\u2014', '-', '\u2014', 'none', ''):
            return None   # sentinel: no target
        try:
            if '/' in text:
                a, t = text.split('/', 1)
                a = float(a.replace('\u20b9', '').replace(',', '').strip() or 0)
                t = float(t.replace('\u20b9', '').replace(',', '').strip() or 0)
                return (a / t * 100) if t else 0.0
            return float(text.replace('%', ''))
        except:
            return 0.0

    def pct_class(p, zero=False):
        if p is None or zero:
            return 'zero'
        if p >= 100:
            return 'green'
        if p >= 70:
            return 'amber'
        if p >= 40:
            return 'orange'
        return 'red'

    def pill(pct_val, zero=False):
        p = pct_value(pct_val)
        if p is None:
            return '<span class="pct zero">—</span>'
        return f'<span class="pct {pct_class(p, zero)}">{esc(pct_val)}</span>'

    def online_pct(a, t):
        if t is None:
            return "—"
        a = 0 if pd.isna(a) else a
        t = 0 if pd.isna(t) else t
        if isinstance(a, float) and a.is_integer():
            a = int(a)
        if isinstance(t, float) and t.is_integer():
            t = int(t)
        return f"{(a / t * 100):.1f}%" if t else "0.0%"

    gt = sup_rev[sup_rev['Supervisor'].astype(str).str.contains('Grand Total', na=False)].iloc[0]
    gt_adm = sup_adm[sup_adm['Supervisor'].astype(str).str.contains('Grand Total', na=False)].iloc[0]
    total_adm_target = sum(adm_targets.values())
    adm_ach_pct = (float(gt_adm['Achieve']) / total_adm_target * 100) if total_adm_target else 0

    # ── Pre-build row HTML for supervisor snapshot cards ──
    # Maps display name → sheet config key (used for target lookups in Google Sheets)
    # Dynamic — driven by Online_Targets sheet; display name = sheet key name
    sup_card_html = ''
    sup_summary_rows = ''

    for disp_name in list(adm_targets.keys()):
        fee_row = sup_rev[sup_rev['Supervisor'] == disp_name]
        adm_row = sup_adm[sup_adm['Supervisor'] == disp_name]
        if fee_row.empty or adm_row.empty:
            continue
        fee_row = fee_row.iloc[0]
        adm_row = adm_row.iloc[0]

        fee_ach = float(fee_row['Fee Collected'])
        fee_tgt = float(fee_row['Target'])
        fee_pct_val = (fee_ach / fee_tgt * 100) if fee_tgt else 0
        fee_pct_class = pct_class(fee_pct_val)
        fee_pct_str = online_pct(fee_ach, fee_tgt)

        adm_ach_val = int(adm_row['Achieve'])
        adm_tgt = adm_targets.get(disp_name, 0)
        ftd_fee = float(fee_row['FTD'])
        ftd_adm = int(adm_row['FTD'])
        bar_width = min(fee_pct_val, 100)
        bar_color = 'green' if fee_pct_val >= 100 else ('amber' if fee_pct_val >= 70 else ('orange' if fee_pct_val >= 40 else 'red'))

        sup_card_html += f'''<div class="sup-card {disp_name.lower()}">
<div class="sup-name"><small>Team Owner</small>{disp_name}</div>
<div class="sup-row"><span class="sup-metric">Fee Collected</span><span class="sup-val">{money(fee_ach)} / {money(fee_tgt)}</span></div>
<div class="sup-row"><span class="sup-metric">Fee Ach %</span><span class="sup-val {bar_color}">{fee_pct_str}</span></div>
<div class="sup-row"><span class="sup-metric">Admissions</span><span class="sup-val">{adm_ach_val}</span></div>
<div class="sup-row"><span class="sup-metric">FTD</span><span class="sup-val">{num(ftd_adm) if ftd_adm else 0} adm &middot; {money(ftd_fee)}</span></div>
<div class="sup-bar-bg"><div class="sup-bar" style="width:{bar_width}%"></div></div></div>\n'''

        # Summary table row
        sup_summary_rows += f'''<tr><td class="left bold">{disp_name}</td><td>{adm_ach_val}</td><td class="rev">{money_full(fee_tgt)}</td><td class="rev">{money_full(fee_ach)}</td><td>{pill(fee_pct_str)}</td><td class="ftd">{money_full(ftd_fee)}</td><td>{num(ftd_adm) if ftd_adm else 0}</td></tr>\n'''

    # Grand totals
    gt_fee_ach = float(gt['Fee Collected'])
    gt_fee_tgt = float(gt['Target'])
    gt_fee_ftd = float(gt['FTD'])
    gt_adm_ach = int(gt_adm['Achieve'])
    gt_adm_ftd = int(float(gt_adm['FTD'])) if float(gt_adm['FTD']) else 0
    gt_fee_pct_str = online_pct(gt_fee_ach, gt_fee_tgt)

    sup_summary_rows += f'''
<tr class="grand-total"><td class="left bold">&#9679; Grand Total</td><td>{gt_adm_ach}</td><td class="rev">{money_full(gt_fee_tgt)}</td><td class="rev">{money_full(gt_fee_ach)}</td><td>{pill(gt_fee_pct_str)}</td><td class="ftd">{money_full(gt_fee_ftd)}</td><td>{num(gt_adm_ftd)}</td></tr>'''

    # ── Pre-build counsellor fee rows ──
    c_rev_rows = ''
    for sup_name in list(adm_targets.keys()):
        team = c_rev[(c_rev['Supervisor'] == sup_name) &
                     ~c_rev['Counsellor'].astype(str).str.contains('Total', na=False) &
                     (c_rev['Counsellor'].astype(str) != 'nan') &
                     (c_rev['Counsellor'].astype(str) != '')]
        if team.empty:
            continue
        c_rev_rows += f'<tr class="sup-header"><td colspan="5">&#128100; {sup_name} Team</td></tr>\n'
        for _, r in team.iterrows():
            if str(r['Counsellor']) == 'nan':
                continue
            c_rev_rows += f'<tr><td class="left">{esc(r["Counsellor"])}</td><td class="rev">{money_full(r["Target"])}</td><td class="rev">{money_full(r["Fee Collected"])}</td><td>{pill(r["Ach %"], True)}</td><td class="ftd">{money_full(r["FTD"])}</td></tr>\n'
        sub_ach = team['Fee Collected'].sum()
        sub_tgt = team['Target'].sum()
        sub_ftd = team['FTD'].sum()
        c_rev_rows += f'<tr class="sub-total"><td class="left bold">Total ({sup_name})</td><td class="rev">{money_full(sub_tgt)}</td><td class="rev">{money_full(sub_ach)}</td><td>{pill(online_pct(sub_ach, sub_tgt), True)}</td><td class="ftd">{money_full(sub_ftd)}</td></tr>\n'

    # Filter out subtotal/grand total rows from c_rev (they contain "Total" or NaN in Counsellor column)
    c_rev_clean = c_rev[~c_rev['Counsellor'].astype(str).str.contains('Total', na=False) & (c_rev['Counsellor'].astype(str) != 'nan') & (c_rev['Counsellor'].astype(str) != '')]
    total_tgt = c_rev_clean['Target'].sum()
    total_ach = c_rev_clean['Fee Collected'].sum()
    total_ftd_rev = c_rev_clean['FTD'].sum()
    c_rev_rows += f'<tr class="grand-total"><td class="left bold">&#11007; Grand Total</td><td class="rev">{money_full(total_tgt)}</td><td class="rev">{money_full(total_ach)}</td><td>{pill(online_pct(total_ach, total_tgt))}</td><td class="ftd">{money_full(total_ftd_rev)}</td></tr>\n'

    # ── Pre-build counsellor admission rows ──
    c_adm_rows = ''
    for sup_name in list(adm_targets.keys()):
        team = c_adm[(c_adm['Supervisor'] == sup_name) &
                     ~c_adm['Counsellor'].astype(str).str.contains('Total', na=False) &
                     (c_adm['Counsellor'].astype(str) != 'nan') &
                     (c_adm['Counsellor'].astype(str) != '')]
        if team.empty:
            continue
        c_adm_rows += f'<tr class="sup-header"><td colspan="3">&#128100; {sup_name} Team</td></tr>\n'
        for _, r in team.iterrows():
            if str(r['Counsellor']) == 'nan':
                continue
            ach = int(float(r['Achieve'])) if float(r['Achieve']) else '\u2014'
            ftd_val = int(float(r['FTD'])) if float(r['FTD']) else '\u2014'
            c_adm_rows += f'<tr><td class="left">{esc(r["Counsellor"])}</td><td>{ach if isinstance(ach, int) else "\u2014"}</td><td class="ftd">{ftd_val if isinstance(ftd_val, int) else "\u2014"}</td></tr>\n'
        sub_a = int(team['Achieve'].sum())
        sub_f = int(team['FTD'].sum())
        c_adm_rows += f'<tr class="sub-total"><td class="left bold">Total ({sup_name})</td><td>{sub_a}</td><td class="ftd">{sub_f if sub_f else "\u2014"}</td></tr>\n'
    c_adm_clean = c_adm[~c_adm['Counsellor'].astype(str).str.contains('Total', na=False) & (c_adm['Counsellor'].astype(str) != 'nan') & (c_adm['Counsellor'].astype(str) != '')]
    total_ach_a = int(c_adm_clean['Achieve'].sum())
    total_ftd_a = int(c_adm_clean['FTD'].sum())
    c_adm_rows += f'<tr class="grand-total"><td class="left bold">&#11007; Grand Total</td><td>{total_ach_a}</td><td class="ftd">{total_ftd_a}</td></tr>\n'

    # ── Pre-build counsellor targets vs achievements rows (combined fee + adm) ──
    c_tva_rows = ''
    for sup_name in list(adm_targets.keys()):
        rev_team = c_rev[(c_rev['Supervisor'] == sup_name) &
                         ~c_rev['Counsellor'].astype(str).str.contains('Total', na=False) &
                         (c_rev['Counsellor'].astype(str) != 'nan') &
                         (c_rev['Counsellor'].astype(str) != '')]
        if rev_team.empty:
            continue
        c_tva_rows += f'<tr class="sup-header"><td colspan="8">&#128100; {sup_name} Team</td></tr>\n'
        for _, r in rev_team.iterrows():
            couns = str(r['Counsellor'])
            if couns == 'nan':
                continue
            adm_match = c_adm[c_adm['Counsellor'] == couns]
            adm_mtd = int(float(adm_match.iloc[0]['Achieve'])) if not adm_match.empty else 0
            adm_ftd_v = int(float(adm_match.iloc[0]['FTD'])) if not adm_match.empty else 0
            c_tva_rows += (
                f'<tr>'
                f'<td class="left">{esc(couns)}</td>'
                f'<td class="rev">{money_full(r["Target"])}</td>'
                f'<td class="rev">{money_full(r["Fee Collected"])}</td>'
                f'<td>{pill(r["Ach %"], True)}</td>'
                f'<td class="ftd">{money_full(r["FTD"])}</td>'
                f'<td>{adm_mtd if adm_mtd else chr(8212)}</td>'
                f'<td class="ftd">{adm_ftd_v if adm_ftd_v else chr(8212)}</td>'
                f'</tr>\n'
            )
        adm_team = c_adm[(c_adm['Supervisor'] == sup_name) &
                         ~c_adm['Counsellor'].astype(str).str.contains('Total', na=False) &
                         (c_adm['Counsellor'].astype(str) != 'nan') &
                         (c_adm['Counsellor'].astype(str) != '')]
        sub_fee_tgt = rev_team['Target'].sum()
        sub_fee_ach = rev_team['Fee Collected'].sum()
        sub_fee_ftd = rev_team['FTD'].sum()
        sub_adm_mtd = int(adm_team['Achieve'].sum()) if not adm_team.empty else 0
        sub_adm_ftd = int(adm_team['FTD'].sum()) if not adm_team.empty else 0
        c_tva_rows += (
            f'<tr class="sub-total">'
            f'<td class="left bold">Total ({sup_name})</td>'
            f'<td class="rev">{money_full(sub_fee_tgt)}</td>'
            f'<td class="rev">{money_full(sub_fee_ach)}</td>'
            f'<td>{pill(online_pct(sub_fee_ach, sub_fee_tgt), True)}</td>'
            f'<td class="ftd">{money_full(sub_fee_ftd)}</td>'
            f'<td>{sub_adm_mtd}</td>'
            f'<td class="ftd">{sub_adm_ftd if sub_adm_ftd else chr(8212)}</td>'
            f'</tr>\n'
        )
    c_rev_clean_tva = c_rev[~c_rev['Counsellor'].astype(str).str.contains('Total', na=False) & (c_rev['Counsellor'].astype(str) != 'nan') & (c_rev['Counsellor'].astype(str) != '')]
    c_adm_clean_tva = c_adm[~c_adm['Counsellor'].astype(str).str.contains('Total', na=False) & (c_adm['Counsellor'].astype(str) != 'nan') & (c_adm['Counsellor'].astype(str) != '')]
    gt_tva_fee_tgt = float(c_rev_clean_tva['Target'].sum())
    gt_tva_fee_ach = float(c_rev_clean_tva['Fee Collected'].sum())
    gt_tva_fee_ftd = float(c_rev_clean_tva['FTD'].sum())
    gt_tva_adm_mtd = int(c_adm_clean_tva['Achieve'].sum())
    gt_tva_adm_ftd = int(c_adm_clean_tva['FTD'].sum())
    c_tva_rows += (
        f'<tr class="grand-total">'
        f'<td class="left bold">&#11007; Grand Total</td>'
        f'<td class="rev">{money_full(gt_tva_fee_tgt)}</td>'
        f'<td class="rev">{money_full(gt_tva_fee_ach)}</td>'
        f'<td>{pill(online_pct(gt_tva_fee_ach, gt_tva_fee_tgt))}</td>'
        f'<td class="ftd">{money_full(gt_tva_fee_ftd)}</td>'
        f'<td>{gt_tva_adm_mtd}</td>'
        f'<td class="ftd">{gt_tva_adm_ftd if gt_tva_adm_ftd else chr(8212)}</td>'
        f'</tr>\n'
    )

    # ── Pre-build counsellor last activity rows (supervisor-grouped) ──
    def _status_badge(days):
        if days is None:
            return '<span style="color:#8896a8">—</span>'
        label = f'{days} Days'
        if days <= 2:
            return f'<span class="pct green">{label}</span>'
        if days <= 7:
            return f'<span class="pct amber">{label}</span>'
        if days <= 14:
            return f'<span class="pct orange">{label}</span>'
        return f'<span class="pct red">{label}</span>'

    def _safe_days(d):
        return int(d) if d is not None and not (isinstance(d, float) and math.isnan(d)) else None

    last_act_rows = ''
    if not last_activity.empty:
        for sup_name, grp in last_activity.groupby('Supervisor', sort=False):
            last_act_rows += f'<tr class="sup-header"><td colspan="6">&#128100; {esc(sup_name)} Team</td></tr>\n'
            for _, r in grp.iterrows():
                cn       = str(r['Counsellor'])
                last_adm = esc(str(r['Last Admission']))  if r['Last Admission'] not in (None, '—', float('nan')) else '—'
                days_adm = r['Days Since Adm']
                last_app = esc(str(r['Last Application'])) if r['Last Application'] not in (None, '—', float('nan')) else '—'
                days_app = r['Days Since App']
                adm_badge = _status_badge(_safe_days(days_adm)) if last_adm != '—' else '<span style="color:#8896a8">—</span>'
                app_badge = _status_badge(_safe_days(days_app)) if last_app != '—' else '<span style="color:#8896a8">—</span>'

                last_act_rows += (
                    f'<tr>'
                    f'<td class="left">{esc(cn)}</td>'
                    f'<td>{last_adm}</td>'
                    f'<td>{adm_badge}</td>'
                    f'<td>{last_app}</td>'
                    f'<td>{app_badge}</td>'
                    f'</tr>\n'
                )

    # ── Pre-build college rows ──
    college_rows = ''
    for _, r in college.iterrows():
        college_rows += f'<tr><td class="left bold">{esc(r["Colleges"])}</td>'
        for col in ['YTD Forms', 'YTD Admissions', 'YTD F2A %', 'MTD Forms', 'MTD Admissions', 'MTD F2A %', 'FTD Forms', 'FTD Admissions', 'FTD F2A %']:
            v = r[col]
            if 'F2A' in col or 'Ach %' in col or '%' in col:
                college_rows += f'<td>{pill(v)}</td>'
            else:
                college_rows += f'<td class="ytd">{num(v)}</td>'
        college_rows += '</tr>\n'

    ytd_f = int(college['YTD Forms'].sum()) if not college.empty else 0
    ytd_a = int(college['YTD Admissions'].sum()) if not college.empty else 0
    ytd_f2a_pct = online_pct(ytd_a, ytd_f)
    ytd_f2a_class = pct_class((ytd_a / ytd_f * 100) if ytd_f else 0)

    # ── Assemble final HTML (concatenation of small f-strings avoids the {{}} f-string nesting bug) ──
    _CSS_BASE_ONLINE = r'''
.sup-ratio, .kpi-ratio { font-size: 11px; opacity: 0.8; margin-top: 2px; font-weight: 400; }
.sup-val-ratio { font-size: 10px; display: block; opacity: 0.7; }
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}:root{--white:#fff;--off:#f0ede8;--border:#d8d2c8;--border-dark:#b0a898;--ink:#111110;--ink-mid:#444440;--ink-light:#7a7570;--gold:#c8a84b;--gold-light:#f0e4c0;--green:#1e6b3c;--green-bg:#d4eddf;--amber:#a05e10;--amber-bg:#faecd4;--orange:#b04800;--orange-bg:#faddcc;--red:#b02020;--red-bg:#fad4d4;--gray:#777770;--gray-bg:#e8e6e2;--blue:#1a4a8a;--blue-bg:#e8eef7;--radius:3px}body{background:#f4f2ee;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:var(--ink);min-height:100vh;overflow-x:hidden}#t1,#t2,#t3,#t4,#t5{display:none}.shell{max-width:1400px;margin:0 auto;padding:0 28px 48px}.header{padding:26px 0 18px;border-bottom:3px solid var(--ink);margin-bottom:22px}.header-top{display:flex;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;gap:8px}.brand{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--ink-light);margin-bottom:5px}.title{font-size:clamp(20px,4.5vw,30px);font-weight:700;letter-spacing:-.01em;line-height:1.15}.header-meta{text-align:right}.badge{display:inline-block;background:var(--ink);color:var(--white);font-size:9px;letter-spacing:.14em;text-transform:uppercase;padding:3px 8px;border-radius:var(--radius);margin-bottom:4px}.date{font-size:11px;color:var(--ink-light);letter-spacing:.04em}.tabs{display:grid;grid-template-columns:1fr 1fr 1fr 1fr 1fr;gap:6px;margin-bottom:22px;position:sticky;top:0;z-index:100;background:#f4f2ee;padding:8px 0}.tab-label{display:flex;align-items:center;justify-content:center;gap:4px;padding:9px 6px;cursor:pointer;font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--ink-mid);background:var(--off);border:1.5px solid var(--border-dark);border-radius:var(--radius);user-select:none;-webkit-tap-highlight-color:transparent;white-space:nowrap}#t1:checked~.shell label[for=t1],#t2:checked~.shell label[for=t2],#t3:checked~.shell label[for=t3],#t4:checked~.shell label[for=t4],#t5:checked~.shell label[for=t5]{background:var(--ink);color:var(--white);border-color:var(--ink)}.panel{display:none}#t1:checked~.shell #p1,#t2:checked~.shell #p2,#t3:checked~.shell #p3,#t4:checked~.shell #p4,#t5:checked~.shell #p5{display:block}.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin-bottom:24px}.kpi{background:var(--white);border:1px solid var(--border);border-radius:var(--radius);padding:16px 18px;border-left:4px solid var(--ink)}.kpi-label{font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--ink-light);margin-bottom:3px}.kpi-value{font-size:26px;font-weight:700;line-height:1.1;letter-spacing:-.02em}.kpi-value.orange{color:var(--orange)}.kpi-value.green{color:var(--green)}.kpi-value.amber{color:var(--amber)}.kpi-value.red{color:var(--red)}.kpi-sub{font-size:11px;color:var(--ink-mid);margin-top:2px}.slabel{font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--ink);margin-bottom:12px;padding-bottom:6px;border-bottom:2px solid var(--ink)}.sup-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px;margin-bottom:24px}.sup-card{background:var(--white);border:1px solid var(--border);border-radius:5px;padding:11px 12px 10px;border-top:3px solid var(--ink)}.sup-name{font-size:14px;font-weight:700;margin-bottom:6px;letter-spacing:-.01em}.sup-name small{display:block;font-size:9px;font-weight:500;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-light);margin-bottom:1px}.sup-row{display:flex;justify-content:space-between;align-items:baseline;padding:2px 0;font-size:12px}.sup-metric{color:var(--ink-mid);font-size:10px}.sup-val{font-weight:700;white-space:nowrap;font-size:12px}.sup-val.orange{color:var(--orange)}.sup-val.red{color:var(--red)}.sup-val.green{color:var(--green)}.sup-val.amber{color:var(--amber)}.sup-bar-bg{background:var(--border);height:5px;border-radius:2px;margin-top:8px;overflow:hidden}.sup-bar{height:100%;border-radius:2px;background:var(--green);transition:width .4s}.table-wrap{overflow-x:auto;border-radius:4px;border:2px solid var(--border-dark);background:var(--white)}table{width:100%;border-collapse:collapse;font-size:13px}th{background:#1a1a18;color:#ffffff;padding:10px 8px;text-align:center;font-weight:700;font-size:11px;letter-spacing:.05em;border-right:1px solid rgba(255,255,255,.15)}th:last-child{border-right:none}th.left,td.left{text-align:left;padding-left:12px}td{text-align:center;padding:8px 8px;border-bottom:1px solid var(--border);white-space:nowrap;font-variant-numeric:tabular-nums;font-size:13px}tbody tr:nth-child(even) td{background:#f7f5f1}tbody tr:nth-child(odd) td{background:#ffffff}tr:last-child td{border-bottom:none}tbody tr:hover td{background:#eef0f8}.bold{font-weight:700}.rev{font-variant-numeric:tabular-nums;font-weight:600}.ftd{color:var(--ink-mid)}.ytd{font-weight:600}.pct{font-weight:700;font-size:11px;padding:3px 8px;border-radius:3px;display:inline-block}.pct.green{background:var(--green-bg);color:var(--green)}.pct.amber{background:var(--amber-bg);color:var(--amber)}.pct.orange{background:var(--orange-bg);color:var(--orange)}.pct.red{background:var(--red-bg);color:var(--red)}.pct.zero{color:var(--gray);background:var(--gray-bg)}.sup-header td{background:#f0e4c0!important;color:#5a3e00;font-weight:700;border-top:2px solid var(--gold)!important;padding:6px 12px!important;font-size:11px;letter-spacing:.06em;text-transform:uppercase}.sub-total td{background:#e8e4de!important;border-top:2px solid var(--border-dark)!important;font-weight:700!important}.grand-total td{font-weight:700!important;border-top:3px solid var(--ink)!important;background:#e0ddd8!important;font-size:13.5px!important}.varun .sup-bar{background:var(--blue)}.sunil .sup-bar{background:var(--green)}.vishal .sup-bar{background:var(--orange)}.siddhartha .sup-bar{background:var(--amber)}.legend{display:flex;gap:16px;margin-top:14px;font-size:11px;color:var(--ink-mid)}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}.dot.green{background:var(--green)}.dot.amber{background:var(--amber)}.dot.orange{background:var(--orange)}.dot.red{background:var(--red)}.footer{margin-top:28px;padding:16px 0 8px;border-top:1px solid var(--border);font-size:11px;color:var(--ink-light);text-align:center}.footer-brand{font-weight:600;color:var(--ink-mid);margin-top:4px}@media(max-width:600px){.kpis{grid-template-columns:1fr 1fr}.kpi-value{font-size:22px}table{font-size:12px}.sup-cards{grid-template-columns:1fr}.header-top{flex-wrap:nowrap;align-items:flex-start}.header-meta{flex-shrink:0;min-width:fit-content}}
'''

    _CSS_COLORFUL_ONLINE = r'''
.sup-ratio,.kpi-ratio{font-size:11px;opacity:.8;margin-top:2px;font-weight:400}
.sup-val-ratio{font-size:10px;display:block;opacity:.65}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --navy:#0d1b2a;--navy-mid:#1b2e45;--navy-light:#243b55;
  --teal:#0097a7;--teal-light:#e0f7fa;
  --white:#ffffff;--surface:#f7f9fc;--surface2:#eef2f7;
  --border:#dce3ed;--border-strong:#b8c4d4;
  --ink:#0d1b2a;--ink-mid:#4a5568;--ink-light:#8896a8;
  --green:#1a7f4b;--green-bg:#e3f5ec;
  --amber:#b45309;--amber-bg:#fef3c7;
  --orange:#c2410c;--orange-bg:#ffedd5;
  --red:#b91c1c;--red-bg:#fee2e2;
  --gray:#6b7280;--gray-bg:#f3f4f6;
  --radius:5px}
body{background:var(--surface);font-family:'Segoe UI',-apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif;color:var(--ink);min-height:100vh;overflow-x:hidden}
#t1,#t2,#t3,#t4,#t5{display:none}
.shell{max-width:1400px;margin:0 auto;padding:0 28px 52px}
.header{background:var(--navy);padding:28px 28px 24px;border-radius:8px;margin-bottom:20px;border-left:5px solid var(--teal)}
.header-top{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}
.brand{font-size:9px;letter-spacing:.22em;text-transform:uppercase;color:var(--teal);margin-bottom:6px;font-weight:600}
.title{font-size:clamp(18px,3.5vw,26px);font-weight:700;line-height:1.2;color:#fff;letter-spacing:-.01em}
.header-meta{text-align:right}
.badge{display:inline-block;background:var(--teal);color:#fff;font-size:9px;letter-spacing:.12em;text-transform:uppercase;padding:4px 12px;border-radius:20px;margin-bottom:5px;font-weight:600}
.date{font-size:11px;color:rgba(255,255,255,.55);letter-spacing:.04em}
.tabs{display:grid;grid-template-columns:1fr 1fr 1fr 1fr 1fr;gap:8px;margin-bottom:20px;position:sticky;top:0;z-index:100;background:var(--surface);padding:10px 0}
.tab-label{display:flex;align-items:center;justify-content:center;gap:5px;padding:10px 6px;cursor:pointer;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-mid);background:var(--white);border:1.5px solid var(--border-strong);border-radius:var(--radius);user-select:none;white-space:nowrap;font-weight:600;transition:all .15s}
#t1:checked~.shell label[for=t1],#t2:checked~.shell label[for=t2],#t3:checked~.shell label[for=t3],#t4:checked~.shell label[for=t4],#t5:checked~.shell label[for=t5]{background:var(--navy);color:#fff;border-color:var(--navy)}
.panel{display:none}
#t1:checked~.shell #p1,#t2:checked~.shell #p2,#t3:checked~.shell #p3,#t4:checked~.shell #p4,#t5:checked~.shell #p5{display:block}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px;margin-bottom:24px}
.kpi{background:var(--white);border:1px solid var(--border);border-radius:var(--radius);padding:18px 20px;border-top:3px solid var(--teal)}
.kpi:nth-child(2){border-top-color:#1a7f4b}.kpi:nth-child(3){border-top-color:#b45309}
.kpi-label{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--ink-light);margin-bottom:4px;font-weight:600}
.kpi-value{font-size:26px;font-weight:700;line-height:1.1;letter-spacing:-.02em;color:var(--ink)}
.kpi-value.orange{color:var(--orange)}.kpi-value.green{color:var(--green)}.kpi-value.amber{color:var(--amber)}.kpi-value.red{color:var(--red)}
.kpi-sub{font-size:11px;color:var(--ink-light);margin-top:3px}
.slabel{font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--navy-light);margin:20px 0 12px;padding:0 0 8px;border-bottom:2px solid var(--teal);display:flex;align-items:center;gap:8px}
.slabel::before{content:'';display:inline-block;width:4px;height:14px;background:var(--teal);border-radius:2px}
.sup-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px;margin-bottom:22px}
.sup-card{background:var(--white);border:1px solid var(--border);border-radius:var(--radius);padding:14px 15px 12px;border-left:4px solid var(--teal)}
.sup-name{font-size:13px;font-weight:700;margin-bottom:8px;color:var(--navy)}.sup-name small{display:block;font-size:9px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-light);margin-bottom:2px}
.sup-row{display:flex;justify-content:space-between;align-items:baseline;padding:3px 0;font-size:12px;border-bottom:1px solid var(--border)}
.sup-row:last-of-type{border-bottom:none}
.sup-metric{color:var(--ink-light);font-size:10px;font-weight:500}
.sup-val{font-weight:700;white-space:nowrap;font-size:12px;color:var(--ink)}
.sup-val.orange{color:var(--orange)}.sup-val.red{color:var(--red)}.sup-val.green{color:var(--green)}.sup-val.amber{color:var(--amber)}
.sup-bar-bg{background:var(--surface2);height:5px;border-radius:3px;margin-top:10px;overflow:hidden}
.sup-bar{height:100%;border-radius:3px;background:var(--teal)}
.table-wrap{overflow-x:auto;border-radius:6px;border:1px solid var(--border-strong);background:var(--white)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:var(--navy);color:#fff;padding:11px 10px;text-align:center;font-weight:600;font-size:11px;letter-spacing:.05em;border-right:1px solid rgba(255,255,255,.1)}
th:last-child{border-right:none}th.left,td.left{text-align:left;padding-left:14px}
td{text-align:center;padding:9px 10px;border-bottom:1px solid var(--border);white-space:nowrap;font-variant-numeric:tabular-nums;font-size:13px;color:var(--ink)}
tbody tr:nth-child(even) td{background:#f9fbff}
tbody tr:nth-child(odd) td{background:var(--white)}
tr:last-child td{border-bottom:none}
tbody tr:hover td{background:#eef4ff}
.bold{font-weight:700}.rev{font-variant-numeric:tabular-nums;font-weight:600}.ftd{color:var(--ink-light)}.ytd{font-weight:600}
.pct{font-weight:700;font-size:11px;padding:3px 9px;border-radius:4px;display:inline-block;letter-spacing:.02em}
.pct.green{background:var(--green-bg);color:var(--green)}.pct.amber{background:var(--amber-bg);color:var(--amber)}.pct.orange{background:var(--orange-bg);color:var(--orange)}.pct.red{background:var(--red-bg);color:var(--red)}.pct.zero{color:var(--gray);background:var(--gray-bg)}
.sup-header td{background:#e8f4f8!important;color:#0d5c6e;font-weight:700;border-top:2px solid var(--teal)!important;padding:7px 14px!important;font-size:10.5px;letter-spacing:.08em;text-transform:uppercase}
.sub-total td{background:var(--surface2)!important;border-top:1px solid var(--border-strong)!important;font-weight:700!important}
.grand-total td{font-weight:700!important;border-top:2px solid var(--navy)!important;background:var(--navy-light)!important;color:#fff!important;font-size:13px!important}
.varun .sup-card{border-left-color:#0d6efd}.sunil .sup-card{border-left-color:#1a7f4b}.vishal .sup-card{border-left-color:#c2410c}.siddhartha .sup-card{border-left-color:#b45309}.vartika .sup-card{border-left-color:#7c3aed}
.varun .sup-bar{background:#0d6efd}.sunil .sup-bar{background:#1a7f4b}.vishal .sup-bar{background:#c2410c}.siddhartha .sup-bar{background:#b45309}.vartika .sup-bar{background:#7c3aed}
.legend{display:flex;gap:18px;margin-top:14px;font-size:11px;color:var(--ink-light);flex-wrap:wrap}.dot{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:5px}.dot.green{background:var(--green)}.dot.amber{background:var(--amber)}.dot.orange{background:var(--orange)}.dot.red{background:var(--red)}
.footer{margin-top:32px;padding:16px 0 8px;border-top:1px solid var(--border);font-size:11px;color:var(--ink-light);text-align:center}.footer-brand{font-weight:600;color:var(--ink-mid);margin-top:4px}
@media(max-width:600px){.kpis{grid-template-columns:1fr 1fr}.kpi-value{font-size:22px}table{font-size:12px}.sup-cards{grid-template-columns:1fr 1fr}}
'''

    _CSS_SIDEBAR_ONLINE = '.layout{display:flex;gap:16px;align-items:flex-start}.sidebar{width:155px;flex-shrink:0;position:sticky;top:10px;display:flex;flex-direction:column;gap:6px}.sidebar .tab-label{justify-content:flex-start;padding:10px 12px;white-space:normal;text-align:left}.content{flex:1;min-width:0}'
    CSS = (_CSS_COLORFUL_ONLINE if COLORFUL_MODE else _CSS_BASE_ONLINE) + ((_CSS_SIDEBAR_ONLINE) if SIDEBAR_NAV else '')

    # ── Month-over-Month helpers ──
    def _mom_change_cell(chg):
        try: chg = int(float(chg))
        except: chg = 0
        arrow = '▲' if chg > 0 else ('▼' if chg < 0 else '—')
        color = 'green' if chg > 0 else ('red' if chg < 0 else 'gray')
        return f'<span style="color:var(--{color},#666);font-weight:700">{arrow} {abs(chg)}</span>'

    def _mom_fee_change_cell(chg):
        try: chg = float(chg)
        except: chg = 0.0
        arrow = '▲' if chg > 0 else ('▼' if chg < 0 else '—')
        color = 'green' if chg > 0 else ('red' if chg < 0 else 'gray')
        return f'<span style="color:var(--{color},#666);font-weight:700">{arrow} {money(abs(chg))}</span>'

    def _mom_pct_cell(chg_pct):
        s = str(chg_pct)
        color = 'green' if s.startswith('+') else ('red' if s.startswith('-') else 'gray')
        return f'<span class="pct {color}" style="min-width:64px">{esc(s)}</span>'

    # ── Supervisor MoM rows (adm + fee) ──
    sup_mom_rows_html = ''
    last_lbl = LAST_MONTH_LABEL
    this_lbl = MONTH_LABEL
    if not sup_mom.empty:
        adm_last_col = sup_mom.columns[1]
        adm_this_col = sup_mom.columns[2]
        fee_last_col = sup_mom.columns[5]
        fee_this_col = sup_mom.columns[6]
        for _, r in sup_mom.iterrows():
            is_total = str(r['Supervisor']) == 'Grand Total'
            row_cls  = ' class="grand-total"' if is_total else ''
            sup_mom_rows_html += (
                f'<tr{row_cls}>'
                f'<td class="left bold">{esc(r["Supervisor"])}</td>'
                f'<td>{int(r[adm_last_col])}</td>'
                f'<td>{int(r[adm_this_col])}</td>'
                f'<td>{_mom_change_cell(r["Adm Change"])}</td>'
                f'<td>{_mom_pct_cell(r["Adm %"])}</td>'
                f'<td class="rev">{money(r[fee_last_col])}</td>'
                f'<td class="rev">{money(r[fee_this_col])}</td>'
                f'<td>{_mom_fee_change_cell(r["Fee Change"])}</td>'
                f'<td>{_mom_pct_cell(r["Fee %"])}</td>'
                f'</tr>\n'
            )

    # ── Counsellor MoM rows (grouped by supervisor, adm + fee) ──
    couns_mom_rows_html = ''
    if not couns_mom.empty:
        for sup_name, grp in couns_mom.groupby('Supervisor', sort=False):
            couns_mom_rows_html += f'<tr class="sup-header"><td colspan="9">&#128100; {esc(sup_name)} Team</td></tr>\n'
            for _, r in grp.iterrows():
                couns_mom_rows_html += (
                    f'<tr>'
                    f'<td class="left">{esc(r["Counsellor"])}</td>'
                    f'<td>{int(r[couns_mom.columns[2]])}</td>'
                    f'<td>{int(r[couns_mom.columns[3]])}</td>'
                    f'<td>{_mom_change_cell(r["Adm Change"])}</td>'
                    f'<td>{_mom_pct_cell(r["Adm %"])}</td>'
                    f'<td class="rev">{money(r[couns_mom.columns[6]])}</td>'
                    f'<td class="rev">{money(r[couns_mom.columns[7]])}</td>'
                    f'<td>{_mom_fee_change_cell(r["Fee Change"])}</td>'
                    f'<td>{_mom_pct_cell(r["Fee %"])}</td>'
                    f'</tr>\n'
                )
            sub_la = grp[couns_mom.columns[2]].sum(); sub_ta = grp[couns_mom.columns[3]].sum()
            sub_lf = grp[couns_mom.columns[6]].sum(); sub_tf = grp[couns_mom.columns[7]].sum()
            sub_ca = int(sub_ta) - int(sub_la); sub_cf = sub_tf - sub_lf
            def _spct(c, l): return f"{(c/l*100):+.1f}%" if l else ("+100%" if c > 0 else "N/A")
            couns_mom_rows_html += (
                f'<tr class="sub-total">'
                f'<td class="left bold">Total ({esc(sup_name)})</td>'
                f'<td>{int(sub_la)}</td><td>{int(sub_ta)}</td>'
                f'<td>{_mom_change_cell(sub_ca)}</td><td>{_mom_pct_cell(_spct(sub_ca, sub_la))}</td>'
                f'<td class="rev">{money(sub_lf)}</td><td class="rev">{money(sub_tf)}</td>'
                f'<td>{_mom_fee_change_cell(sub_cf)}</td><td>{_mom_pct_cell(_spct(sub_cf, sub_lf))}</td>'
                f'</tr>\n'
            )

    col_mom_rows_html = ''
    if not col_mom.empty:
        for _, r in col_mom.iterrows():
            is_total = str(r['College']) == 'Total'
            row_cls  = ' class="grand-total"' if is_total else ''
            chg_adm  = int(r['Adm Change']) if not pd.isna(r['Adm Change']) else 0
            chg_fee  = float(r['Fee Change']) if not pd.isna(r['Fee Change']) else 0.0
            col_mom_rows_html += (
                f'<tr{row_cls}>'
                f'<td class="left bold">{esc(r["College"])}</td>'
                f'<td>{int(r[col_mom.columns[1]])}</td>'
                f'<td>{int(r[col_mom.columns[2]])}</td>'
                f'<td>{_mom_change_cell(chg_adm)}</td>'
                f'<td>{_mom_pct_cell(r["Adm %"])}</td>'
                f'<td class="rev">{money(r[col_mom.columns[5]])}</td>'
                f'<td class="rev">{money(r[col_mom.columns[6]])}</td>'
                f'<td>{_mom_fee_change_cell(chg_fee)}</td>'
                f'<td>{_mom_pct_cell(r["Fee %"])}</td>'
                f'</tr>\n'
            )

    p1 = '<input type="radio" name="dash" id="t1" checked><input type="radio" name="dash" id="t2"><input type="radio" name="dash" id="t3"><input type="radio" name="dash" id="t4"><input type="radio" name="dash" id="t5"><input type="radio" name="dash" id="t6"><input type="radio" name="dash" id="t7"><input type="radio" name="dash" id="t8">'
    p2 = f'<div class="shell"><header class="header"><div class="header-top"><div><div class="brand">Performance Intelligence &middot; Online</div><h1 class="title">Online Admissions &amp; Fee Collected Tracker</h1></div><div class="header-meta"><div class="badge">Live Report</div><div class="date">{MONTH_LABEL} &middot; FTD {report_date.strftime("%d %b")}</div></div></div></header>'
    if SIDEBAR_NAV:
        p3 = '<div class="layout"><nav class="sidebar"><label class="tab-label" for="t1">&#x1f4ca; Overview</label><label class="tab-label" for="t2">&#x1f4b0; Fee Collected</label><label class="tab-label" for="t3">&#x1f393; Admissions</label><label class="tab-label" for="t4">&#x1f3eb; Colleges</label><label class="tab-label" for="t5">&#x1f465; Counsellor T vs A</label><label class="tab-label" for="t6">&#x23f0; Last Activity</label><label class="tab-label" for="t7">&#x1f4c8; Supervisor MoM</label><label class="tab-label" for="t8">&#x1f3db;&#xfe0f; University MoM</label></nav><div class="content">'
    else:
        p3 = '<div class="tabs" style="grid-template-columns:repeat(8,1fr)"><label class="tab-label" for="t1">&#x1f4ca; Overview</label><label class="tab-label" for="t2">&#x1f4b0; Fee Collected</label><label class="tab-label" for="t3">&#x1f393; Admissions</label><label class="tab-label" for="t4">&#x1f3eb; Colleges</label><label class="tab-label" for="t5">&#x1f465; Counsellor T vs A</label><label class="tab-label" for="t6">&#x23f0; Last Activity</label><label class="tab-label" for="t7">&#x1f4c8; Supervisor MoM</label><label class="tab-label" for="t8">&#x1f3db;&#xfe0f; University MoM</label></div>'
    p4_overview_kpis = f'<div class="kpis"><div class="kpi"><div class="kpi-label">Total Fee Collected</div><div class="kpi-value {pct_class((gt_fee_ach / gt_fee_tgt * 100) if gt_fee_tgt else 0)}">{money(gt_fee_ach)}</div><div class="kpi-sub">of {money(gt_fee_tgt)} target</div></div><div class="kpi"><div class="kpi-label">Fee Ach %</div><div class="kpi-value {pct_class((gt_fee_ach / gt_fee_tgt * 100) if gt_fee_tgt else 0)}">{gt_fee_pct_str}</div><div class="kpi-ratio">{money(gt_fee_ach).replace(chr(0x20b9), "")}/{money(gt_fee_tgt).replace(chr(0x20b9), "")}</div><div class="kpi-sub">Grand Total</div></div><div class="kpi"><div class="kpi-label">Admissions</div><div class="kpi-value">{gt_adm_ach}</div><div class="kpi-sub">Grand Total</div></div></div>'
    p4 = f'<section class="panel" id="p1">{p4_overview_kpis}<div class="slabel"><span>Team Owner Snapshot</span></div><div class="sup-cards">{sup_card_html}</div><div class="slabel"><span>Team Owner Summary Table</span></div><div class="table-wrap"><table><thead><tr><th class="left">Team Owner</th><th>Adm Ach</th><th>Fee TG</th><th>Fee Ach</th><th>Fee Ach %</th><th>FTD Fee</th><th>FTD Adm</th></tr></thead><tbody>{sup_summary_rows}</tbody></table></div></section>'
    p5_fee_kpis = f'<div class="kpis"><div class="kpi"><div class="kpi-label">Fee Target</div><div class="kpi-value">{money(gt_fee_tgt)}</div><div class="kpi-sub">{MONTH_LABEL}</div></div><div class="kpi"><div class="kpi-label">Achieved</div><div class="kpi-value {pct_class((gt_fee_ach / gt_fee_tgt * 100) if gt_fee_tgt else 0)}">{money(gt_fee_ach)}</div><div class="kpi-sub">{gt_fee_pct_str} overall</div></div><div class="kpi"><div class="kpi-label">FTD</div><div class="kpi-value green">{money(gt_fee_ftd)}</div><div class="kpi-sub">Today\'s fee collected</div></div></div>'
    p5 = f'<section class="panel" id="p2">{p5_fee_kpis}<div class="slabel"><span>Counsellor-wise Fee Collected Breakdown &middot; Counsellor targets are intentionally zero</span></div><div class="table-wrap"><table><thead><tr><th class="left">Counsellor</th><th>Target</th><th>MTD Achieved</th><th>Ach %</th><th>FTD</th></tr></thead><tbody>{c_rev_rows}</tbody></table></div></section>'
    p6_adm_kpis = f'<div class="kpis"><div class="kpi"><div class="kpi-label">Total Admissions</div><div class="kpi-value">{gt_adm_ach}</div><div class="kpi-sub">{MONTH_LABEL}</div></div><div class="kpi"><div class="kpi-label">FTD</div><div class="kpi-value green">{gt_adm_ftd}</div><div class="kpi-sub">Today\'s closes</div></div></div>'
    p6 = f'<section class="panel" id="p3">{p6_adm_kpis}<div class="slabel"><span>Counsellor-wise Admissions</span></div><div class="table-wrap"><table><thead><tr><th class="left">Counsellor</th><th>Achieved</th><th>FTD</th></tr></thead><tbody>{c_adm_rows}</tbody></table></div></section>'
    p7 = f'<section class="panel" id="p4"><div class="slabel"><span>College-wise Performance &middot; Forms to Admissions</span></div><div class="table-wrap"><table><thead><tr><th class="left" rowspan="2">College</th><th colspan="3">Year to Date</th><th colspan="3">Month to Date</th><th colspan="3">FTD</th></tr><tr><th>Forms</th><th>Adm</th><th>F2A %</th><th>Forms</th><th>Adm</th><th>F2A %</th><th>Forms</th><th>Adm</th><th>F2A %</th></tr></thead><tbody>{college_rows}</tbody></table></div></section>'
    p7b_kpis = f'<div class="kpis"><div class="kpi"><div class="kpi-label">Fee Target (MTD)</div><div class="kpi-value">{money(gt_tva_fee_tgt)}</div><div class="kpi-sub">{MONTH_LABEL}</div></div><div class="kpi"><div class="kpi-label">Fee Achieved (MTD)</div><div class="kpi-value {pct_class((gt_tva_fee_ach / gt_tva_fee_tgt * 100) if gt_tva_fee_tgt else 0)}">{money(gt_tva_fee_ach)}</div><div class="kpi-sub">{online_pct(gt_tva_fee_ach, gt_tva_fee_tgt)} overall</div></div><div class="kpi"><div class="kpi-label">Admissions (MTD)</div><div class="kpi-value">{gt_tva_adm_mtd}</div><div class="kpi-sub">Grand Total</div></div><div class="kpi"><div class="kpi-label">FTD Fee</div><div class="kpi-value green">{money(gt_tva_fee_ftd)}</div><div class="kpi-sub">Today</div></div><div class="kpi"><div class="kpi-label">FTD Admissions</div><div class="kpi-value green">{gt_tva_adm_ftd if gt_tva_adm_ftd else 0}</div><div class="kpi-sub">Today</div></div></div>'
    p7b = f'<section class="panel" id="p5">{p7b_kpis}<div class="slabel"><span>Counsellor-wise Targets vs Achievements &middot; Fee &amp; Admissions &middot; {MONTH_LABEL}</span></div><div class="table-wrap"><table><thead><tr><th class="left">Counsellor</th><th>Fee Target</th><th>Fee MTD Ach</th><th>Fee Ach %</th><th>Fee FTD</th><th>Adm MTD</th><th>Adm FTD</th></tr></thead><tbody>{c_tva_rows}</tbody></table></div></section>'
    p7c = f'<section class="panel" id="p6"><div class="slabel"><span>Supervisor-wise Counsellor Last Admission &amp; Last Application &middot; {report_date.strftime("%d %b %Y")}</span></div><div class="table-wrap"><table><thead><tr><th class="left">Counsellor</th><th>Last Admission</th><th>Adm Since</th><th>Last Application</th><th>Application Since</th></tr></thead><tbody>{last_act_rows}</tbody></table></div></section>'
    _lm_range_label  = f'{_lm_first.day} {_lm_first.strftime("%b")} – {_lm_same_day.day} {_lm_same_day.strftime("%b %Y")}'
    _tm_range_label  = f'{report_date.replace(day=1).day} {report_date.strftime("%b")} – {report_date.day} {report_date.strftime("%b %Y")}'
    _mom_note = f'<div style="font-size:11px;color:var(--ink-light,#8896a8);margin-bottom:14px;padding:6px 10px;background:var(--surface2,#eef2f7);border-left:3px solid var(--teal,#0097a7);border-radius:3px">Comparing same day window &nbsp;·&nbsp; <strong>{_lm_range_label}</strong> &nbsp;vs&nbsp; <strong>{_tm_range_label}</strong></div>'
    _sup_mom_thead = f'<tr><th class="left">Team Owner</th><th>Adm {esc(LAST_MONTH_LABEL)}</th><th>Adm {esc(MONTH_LABEL)}</th><th>Adm Δ</th><th>Adm %</th><th>Fee {esc(LAST_MONTH_LABEL)}</th><th>Fee {esc(MONTH_LABEL)}</th><th>Fee Δ</th><th>Fee %</th></tr>'
    _couns_mom_thead = f'<tr><th class="left">Counsellor</th><th>Adm {esc(LAST_MONTH_LABEL)}</th><th>Adm {esc(MONTH_LABEL)}</th><th>Adm Δ</th><th>Adm %</th><th>Fee {esc(LAST_MONTH_LABEL)}</th><th>Fee {esc(MONTH_LABEL)}</th><th>Fee Δ</th><th>Fee %</th></tr>'
    p7d = (
        f'<section class="panel" id="p7">'
        f'<div class="slabel"><span>Team Owner &amp; Counsellor &middot; Admissions + Revenue &middot; {LAST_MONTH_LABEL} vs {MONTH_LABEL}</span></div>'
        f'{_mom_note}'
        f'<div class="slabel" style="margin-top:16px"><span>&#128202; Team Owner Summary</span></div>'
        f'<div class="table-wrap"><table><thead>{_sup_mom_thead}</thead><tbody>{sup_mom_rows_html}</tbody></table></div>'
        f'<div class="slabel" style="margin-top:20px"><span>&#128100; Counsellor Drill-Down</span></div>'
        f'<div class="table-wrap"><table><thead>{_couns_mom_thead}</thead><tbody>{couns_mom_rows_html}</tbody></table></div>'
        f'</section>'
    )
    p7e = (
        f'<section class="panel" id="p8">'
        f'<div class="slabel"><span>Partner University-wise Admissions + Revenue &middot; {LAST_MONTH_LABEL} vs {MONTH_LABEL}</span></div>'
        f'{_mom_note}'
        f'<div class="table-wrap"><table><thead>'
        f'<tr><th class="left" rowspan="2">University</th>'
        f'<th colspan="4">Admissions</th>'
        f'<th colspan="4">Fee Collected</th></tr>'
        f'<tr><th>{esc(LAST_MONTH_LABEL)}</th><th>{esc(MONTH_LABEL)}</th><th>Δ</th><th>%</th>'
        f'<th>{esc(LAST_MONTH_LABEL)}</th><th>{esc(MONTH_LABEL)}</th><th>Δ</th><th>%</th></tr>'
        f'</thead><tbody>{col_mom_rows_html}</tbody></table></div>'
        f'</section>'
    )
    if SIDEBAR_NAV:
        p8 = '</div></div></div></body></html>'   # close .content, .layout, .shell
    else:
        p8 = '</div></body></html>'               # close .shell

    _extra_css = (
        '#t6{display:none}#t6:checked~.shell label[for=t6]{background:var(--navy,#1a1a18);color:#fff;border-color:var(--navy,#1a1a18)}#t6:checked~.shell #p6{display:block}'
        '#t7{display:none}#t7:checked~.shell label[for=t7]{background:var(--navy,#1a1a18);color:#fff;border-color:var(--navy,#1a1a18)}#t7:checked~.shell #p7{display:block}'
        '#t8{display:none}#t8:checked~.shell label[for=t8]{background:var(--navy,#1a1a18);color:#fff;border-color:var(--navy,#1a1a18)}#t8:checked~.shell #p8{display:block}'
    )
    html_doc = f'''<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Online Admissions Dashboard</title><style>{CSS}{_extra_css}</style></head><body>
{p1}{p2}{p3}{p4}{p5}{p6}{p7}{p7b}{p7c}{p7d}{p7e}{p8}'''

    with open(out_path, 'w', encoding='utf-8-sig') as f:
        f.write(html_doc)
    print(f"✅ Online LMS HTML: {out_path}")
    summary = f"Adm: {gt_adm_ach} ({adm_ach_pct:.1f}%), Fee: {money(gt_fee_ach)}, FTD Adm: {gt_adm_ftd}"
    return out_path, summary


# ═══════════════════════════════════════════════════════════════════════════════
# PART 2 — REGULAR LMS REPORT
# ═══════════════════════════════════════════════════════════════════════════════

regular_config = load_regular_config()

# Week = Monday of report_date's week → report_date (dynamic, no sheet dependency)
_week_monday   = report_date - timedelta(days=report_date.weekday())
REG_WEEK_START = _week_monday.strftime('%Y-%m-%d')
REG_WEEK_END   = FTD_DATE
COLLEGE_TARGETS = regular_config.get("college_targets", {})

# Days and weeks in the report month — used to auto-compute weekly/daily targets
_days_in_month  = calendar.monthrange(report_date.year, report_date.month)[1]
_weeks_in_month = round(_days_in_month / 7)  # 4 for most months (28-31 days)

REGULAR_DB_CONFIGS = [
    {"name": "REGULAR", "host": os.getenv("REGULAR_LMS_DB_HOST"), "port": int(os.getenv("REGULAR_LMS_DB_PORT", "54321")),
     "database": os.getenv("REGULAR_LMS_DB_NAME"), "user": os.getenv("REGULAR_LMS_DB_USER"),
     "password": os.getenv("REGULAR_LMS_DB_PASSWORD")},
    {"name": "CGC", "host": os.getenv("REGULAR_CGC_LMS_DB_HOST"), "port": int(os.getenv("REGULAR_CGC_LMS_DB_PORT", "54321")),
     "database": os.getenv("REGULAR_CGC_LMS_DB_NAME"), "user": os.getenv("REGULAR_CGC_LMS_DB_USER"),
     "password": os.getenv("REGULAR_CGC_LMS_DB_PASSWORD")},
    {"name": "AMITY", "host": os.getenv("REGULAR_AMITY_LMS_DB_HOST"), "port": int(os.getenv("REGULAR_AMITY_LMS_DB_PORT", "54321")),
     "database": os.getenv("REGULAR_AMITY_LMS_DB_NAME"), "user": os.getenv("REGULAR_AMITY_LMS_DB_USER"),
     "password": os.getenv("REGULAR_AMITY_LMS_DB_PASSWORD")},
]

# Base admission SQL — optional college exclusion and YTD start injected per DB
_REG_ADM_SQL = """SELECT DISTINCT ON (s.student_id, uc.course_id)
    s.student_id, uc.university_name AS college_name,
    csj.created_at AT TIME ZONE 'Asia/Kolkata' AS created_at,
    c.counsellor_name,
    sla.utm_campaign
FROM students s
JOIN course_status_journeys csj ON s.student_id = csj.student_id
JOIN university_courses uc ON csj.course_id = uc.course_id
LEFT JOIN counsellors c ON c.counsellor_id = s.assigned_counsellor_id AND c.role = 'l2'
LEFT JOIN LATERAL (
    SELECT utm_campaign FROM student_lead_activities
    WHERE student_id = s.student_id ORDER BY created_at ASC LIMIT 1
) sla ON true
WHERE csj.course_status = 'Admission'
  AND COALESCE(csj.fee_type, '') NOT ILIKE '%partial%'
  AND csj.created_at >= '{ytd_start}'::date
  AND csj.created_at <  '{ytd_end}'::date
  {exclude_clause}
ORDER BY s.student_id, uc.course_id, csj.created_at ASC;"""

# Fallback admission SQL (no utm_campaign) for DBs without student_lead_activities
_REG_ADM_SQL_NOUTM = """SELECT DISTINCT ON (s.student_id, uc.course_id)
    s.student_id, uc.university_name AS college_name,
    csj.created_at AT TIME ZONE 'Asia/Kolkata' AS created_at,
    c.counsellor_name,
    NULL::text AS utm_campaign
FROM students s
JOIN course_status_journeys csj ON s.student_id = csj.student_id
JOIN university_courses uc ON csj.course_id = uc.course_id
LEFT JOIN counsellors c ON c.counsellor_id = s.assigned_counsellor_id AND c.role = 'l2'
WHERE csj.course_status = 'Admission'
  AND COALESCE(csj.fee_type, '') NOT ILIKE '%partial%'
  AND csj.created_at >= '{ytd_start}'::date
  AND csj.created_at <  '{ytd_end}'::date
  {exclude_clause}
ORDER BY s.student_id, uc.course_id, csj.created_at ASC;"""

# Base forms SQL — 'Walkin Marked' added, optional college exclusion and YTD start injected per DB
_REG_FORM_SQL = """SELECT DISTINCT ON (s.student_id, csj.course_id)
    s.student_id, uc.university_name AS college_name,
    csj.created_at AT TIME ZONE 'Asia/Kolkata' AS created_at,
    c.counsellor_name,
    sla.utm_campaign
FROM students s
JOIN course_status_journeys csj ON s.student_id = csj.student_id
JOIN university_courses uc ON csj.course_id = uc.course_id
LEFT JOIN counsellors c ON c.counsellor_id = s.assigned_counsellor_id AND c.role = 'l2'
LEFT JOIN LATERAL (
    SELECT utm_campaign FROM student_lead_activities
    WHERE student_id = s.student_id ORDER BY created_at ASC LIMIT 1
) sla ON true
WHERE LOWER(csj.course_status) IN (
    'form submitted – portal pending', 'form submitted – completed',
    'walkin completed', 'walkin marked',
    'exam/interview scheduled',
    'offer letter/results pending', 'offer letter/results released',
    'ready for admission'
)
  AND csj.created_at >= '{ytd_start}'::date
  AND csj.created_at <  '{ytd_end}'::date
  {exclude_clause}
ORDER BY s.student_id, csj.course_id, csj.created_at ASC;"""

# Fallback forms SQL (no utm_campaign)
_REG_FORM_SQL_NOUTM = """SELECT DISTINCT ON (s.student_id, csj.course_id)
    s.student_id, uc.university_name AS college_name,
    csj.created_at AT TIME ZONE 'Asia/Kolkata' AS created_at,
    c.counsellor_name,
    NULL::text AS utm_campaign
FROM students s
JOIN course_status_journeys csj ON s.student_id = csj.student_id
JOIN university_courses uc ON csj.course_id = uc.course_id
LEFT JOIN counsellors c ON c.counsellor_id = s.assigned_counsellor_id AND c.role = 'l2'
WHERE LOWER(csj.course_status) IN (
    'form submitted – portal pending', 'form submitted – completed',
    'walkin completed', 'walkin marked',
    'exam/interview scheduled',
    'offer letter/results pending', 'offer letter/results released',
    'ready for admission'
)
  AND csj.created_at >= '{ytd_start}'::date
  AND csj.created_at <  '{ytd_end}'::date
  {exclude_clause}
ORDER BY s.student_id, csj.course_id, csj.created_at ASC;"""

# REGULAR DB contains stale rows for CGC and Amity colleges that are
# authoritatively sourced from their dedicated DBs. Exclude them here
# to prevent double-counting.
_REGULAR_EXCLUDE = """AND uc.university_name NOT ILIKE '%Amity%'
  AND uc.university_name NOT ILIKE '%Chandigarh Group%'
  AND uc.university_name NOT ILIKE '%CGC%'
  AND uc.university_name NOT ILIKE '%Landran%'"""

# CGC and AMITY DBs are the sole sources for their colleges — no exclusion needed
_NO_EXCLUDE = ""

# Amity admissions include partial payments (no fee_type filter)
_AMITY_REG_ADM_SQL = """SELECT DISTINCT ON (s.student_id, uc.course_id)
    s.student_id, uc.university_name AS college_name,
    csj.created_at AT TIME ZONE 'Asia/Kolkata' AS created_at,
    c.counsellor_name,
    sla.utm_campaign
FROM students s
JOIN course_status_journeys csj ON s.student_id = csj.student_id
JOIN university_courses uc ON csj.course_id = uc.course_id
LEFT JOIN counsellors c ON c.counsellor_id = s.assigned_counsellor_id AND c.role = 'l2'
LEFT JOIN LATERAL (
    SELECT utm_campaign FROM student_lead_activities
    WHERE student_id = s.student_id ORDER BY created_at ASC LIMIT 1
) sla ON true
WHERE csj.course_status = 'Admission'
  AND csj.created_at >= '{ytd_start}'::date
  AND csj.created_at < '{ytd_end}'::date
ORDER BY s.student_id, uc.course_id, csj.created_at ASC;"""

_AMITY_REG_ADM_SQL_NOUTM = """SELECT DISTINCT ON (s.student_id, uc.course_id)
    s.student_id, uc.university_name AS college_name,
    csj.created_at AT TIME ZONE 'Asia/Kolkata' AS created_at,
    c.counsellor_name,
    NULL::text AS utm_campaign
FROM students s
JOIN course_status_journeys csj ON s.student_id = csj.student_id
JOIN university_courses uc ON csj.course_id = uc.course_id
LEFT JOIN counsellors c ON c.counsellor_id = s.assigned_counsellor_id AND c.role = 'l2'
WHERE csj.course_status = 'Admission'
  AND csj.created_at >= '{ytd_start}'::date
  AND csj.created_at < '{ytd_end}'::date
ORDER BY s.student_id, uc.course_id, csj.created_at ASC;"""


async def regular_get_data():
    ytd_start = f'{report_date.year}-01-01'
    ytd_end   = (report_date + timedelta(days=1)).strftime('%Y-%m-%d')
    all_adm, all_form = [], []
    db_excludes = {
        "REGULAR": _REGULAR_EXCLUDE,
        "CGC":     _NO_EXCLUDE,
        "AMITY":   _NO_EXCLUDE,
    }

    async def _fetch(conn, sql):
        try:
            return pd.DataFrame([dict(r) for r in await conn.fetch(sql)])
        except Exception as e:
            if 'student_lead_activities' in str(e) or 'does not exist' in str(e).lower():
                return None
            raise

    for db in REGULAR_DB_CONFIGS:
        excl = db_excludes.get(db['name'], _NO_EXCLUDE)
        conn = await asyncpg.connect(host=db['host'], port=db['port'], database=db['database'],
                                     user=db['user'], password=db['password'])
        if db['name'] == 'AMITY':
            adm_sql      = _AMITY_REG_ADM_SQL.format(ytd_start=ytd_start, ytd_end=ytd_end)
            adm_sql_fb   = _AMITY_REG_ADM_SQL_NOUTM.format(ytd_start=ytd_start, ytd_end=ytd_end)
        else:
            adm_sql      = _REG_ADM_SQL.format(exclude_clause=excl, ytd_start=ytd_start, ytd_end=ytd_end)
            adm_sql_fb   = _REG_ADM_SQL_NOUTM.format(exclude_clause=excl, ytd_start=ytd_start, ytd_end=ytd_end)
        form_sql     = _REG_FORM_SQL.format(exclude_clause=excl, ytd_start=ytd_start, ytd_end=ytd_end)
        form_sql_fb  = _REG_FORM_SQL_NOUTM.format(exclude_clause=excl, ytd_start=ytd_start, ytd_end=ytd_end)

        df_a = await _fetch(conn, adm_sql)
        if df_a is None:
            print(f"  ℹ️  {db['name']} DB: student_lead_activities not found — utm_campaign will be null")
            df_a = pd.DataFrame([dict(r) for r in await conn.fetch(adm_sql_fb)])

        df_f = await _fetch(conn, form_sql)
        if df_f is None:
            df_f = pd.DataFrame([dict(r) for r in await conn.fetch(form_sql_fb)])

        all_adm.append(df_a)
        all_form.append(df_f)
        await conn.close()

    def norm(name):
        return "Amity University (All Campuses)" if name and "Amity" in name else name

    df_adm  = pd.concat(all_adm).assign(college_name=lambda x: x['college_name'].apply(norm))
    df_form = pd.concat(all_form).assign(college_name=lambda x: x['college_name'].apply(norm))
    return df_adm, df_form


def regular_pct(a, t):
    if isinstance(a, float) and a.is_integer():
        a = int(a)
    if isinstance(t, float) and t.is_integer():
        t = int(t)
    return f"{(a / t * 100):.1f}%" if t else "0.0%"


def _count(df, college, start=None, end=None, exact=None):
    """
    Count rows for a college in a date window.
    No Python-level dedup — SQL already guarantees DISTINCT ON (student_id, course_id),
    and each college comes from exactly one DB (no cross-DB overlap after exclusion).
    Counting distinct (student_id, course_id) pairs correctly handles multi-course students.
    """
    mask = df['college_name'] == college
    if exact:
        mask &= df['date'] == exact
    elif start:
        mask &= df['date'].between(start, end)
    return int(mask.sum())


YTD_START = f'{report_date.year}-01-01'


# ═══════════════════════════════════════════════════════════════════════════════
# PART 2b — AMITY YEAR-OVER-YEAR TOTAL FORMS
# ═══════════════════════════════════════════════════════════════════════════════

AMITY_FORMS_SPREADSHEET_ID = '1pFq4-ElGJ81y7SiaHGA6HspEpDicf93NPsPZ2k32hYU'
_LAST_YEAR_AMITY_TAB        = 'Last Year Amity '       # trailing space is intentional
_LAST_YEAR_AMITY_LEGACY_TAB = 'Last Year Amity Legacy '  # trailing space is intentional

AMITY_CAMPUSES = ['Bangalore AU', 'Gwalior AU', 'Jaipur AU', 'Lucknow AU', 'Mumbai AU', 'Raipur AU', 'Gurgaon AU']


def _norm_amity_campus_db(name):
    # campus_location values: "Amity University Jaipur", "Amity University Gurugram", etc.
    n = (name or '').lower()
    if 'bangalore' in n: return 'Bangalore AU'
    if 'gwalior'   in n: return 'Gwalior AU'
    if 'jaipur'    in n: return 'Jaipur AU'
    if 'lucknow'   in n: return 'Lucknow AU'
    if 'mumbai'    in n: return 'Mumbai AU'
    if 'raipur'    in n: return 'Raipur AU'
    if 'gurugram'  in n or 'gurgaon' in n: return 'Gurgaon AU'
    return None


def _norm_amity_campus_sheet(name):
    n = (name or '').lower().replace(' ', '').replace(',', '')
    if 'bangalore' in n: return 'Bangalore AU'
    if 'gwalior'   in n: return 'Gwalior AU'
    if 'jaipur'    in n: return 'Jaipur AU'
    if 'lucknow'   in n: return 'Lucknow AU'
    if 'mumbai'    in n: return 'Mumbai AU'
    if 'raipur'    in n: return 'Raipur AU'
    if 'gurgaon'   in n or 'gurugram' in n: return 'Gurgaon AU'
    return None


def _parse_amity_sheet(rows, date_col, campus_col, payment_col=None, date_fmt='%d/%m/%Y'):
    """Parse a last-year Amity sheet into records [{campus, date}], counting all total forms."""
    if not rows:
        return []
    records = []
    for row in rows[1:]:
        max_col = max(date_col, campus_col)
        if len(row) <= max_col:
            continue
        campus = _norm_amity_campus_sheet(row[campus_col])
        if not campus:
            continue
        try:
            dt = datetime.strptime(row[date_col].strip(), date_fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
        records.append({'campus': campus, 'date': dt})
    return records


def amity_get_total_forms_last_year():
    """Read last year's total Amity forms from both Google Sheet tabs and combine."""
    service = get_service()
    all_records = []

    # ── Tab 1: "Last Year Amity " (May 2025 onwards) ──────────────────────────
    # Columns: Form Fill Date (col 2), Campus Name (col 4), Payment Type (col 6)
    r1 = service.spreadsheets().values().get(
        spreadsheetId=AMITY_FORMS_SPREADSHEET_ID,
        range=f"'{_LAST_YEAR_AMITY_TAB}'!A1:Z"
    ).execute()
    rows1 = r1.get('values', [])
    if rows1:
        h = rows1[0]
        col_date    = next((i for i, v in enumerate(h) if 'form fill date' in v.lower()), 2)
        col_campus  = next((i for i, v in enumerate(h) if 'campus name'   in v.lower()), 4)
        col_payment = next((i for i, v in enumerate(h) if 'payment type'  in v.lower()), 6)
        recs = _parse_amity_sheet(rows1, col_date, col_campus, col_payment)
        all_records.extend(recs)
        print(f"  [Last Year Amity ] {len(recs)} total forms")

    # ── Tab 2: "Last Year Amity Legacy" (Mar 2025 – May 2025) ─────────────────
    # Columns: ft (col 0), Campus (col 5), Payment (col 13)
    r2 = service.spreadsheets().values().get(
        spreadsheetId=AMITY_FORMS_SPREADSHEET_ID,
        range=f"'{_LAST_YEAR_AMITY_LEGACY_TAB}'!A1:Z"
    ).execute()
    rows2 = r2.get('values', [])
    if rows2:
        h = rows2[0]
        col_date    = next((i for i, v in enumerate(h) if v.strip().lower() == 'ft'),       0)
        col_campus  = next((i for i, v in enumerate(h) if v.strip().lower() == 'campus'),   5)
        col_payment = next((i for i, v in enumerate(h) if v.strip().lower() == 'payment'), 13)
        recs = _parse_amity_sheet(rows2, col_date, col_campus, col_payment)
        all_records.extend(recs)
        print(f"  [Last Year Amity Legacy] {len(recs)} total forms")

    print(f"  [Last Year Total] {len(all_records)} total Amity forms loaded")
    return pd.DataFrame(all_records) if all_records else pd.DataFrame(columns=['campus', 'date'])


_AMITY_TOTAL_FORMS_SQL = """
SELECT DISTINCT ON (s.student_id, csj.course_id)
    s.student_id, uc.university_name AS college_name,
    csj.created_at AT TIME ZONE 'Asia/Kolkata' AS created_at
FROM students s
JOIN course_status_journeys csj ON s.student_id = csj.student_id
JOIN university_courses uc ON csj.course_id = uc.course_id
WHERE LOWER(csj.course_status) IN (
    'form submitted – portal pending', 'form submitted – completed',
    'walkin completed', 'walkin marked',
    'exam/interview scheduled',
    'offer letter/results pending', 'offer letter/results released',
    'ready for admission'
)
  AND COALESCE(csj.fee_type, '') NOT ILIKE '%partial%'
  AND csj.created_at >= '{ytd_start}'::date
ORDER BY s.student_id, csj.course_id, csj.created_at ASC
"""


async def amity_get_total_forms_this_year():
    """Query AMITY DB for this year's Amity total forms via course_status_journeys."""
    db = next(d for d in REGULAR_DB_CONFIGS if d['name'] == 'AMITY')
    ytd_start = f'{report_date.year}-01-01'
    sql = _AMITY_TOTAL_FORMS_SQL.format(ytd_start=ytd_start)
    conn = await asyncpg.connect(host=db['host'], port=db['port'],
                                  database=db['database'], user=db['user'], password=db['password'])
    rows = await conn.fetch(sql)
    await conn.close()
    records = []
    for r in rows:
        campus = _norm_amity_campus_db(r['college_name'])
        if campus:
            records.append({'campus': campus, 'date': r['created_at'].date().isoformat()})
    print(f"  [AMITY DB] {len(records)} total Amity forms loaded")
    return pd.DataFrame(records) if records else pd.DataFrame(columns=['campus', 'date'])


def amity_build_yoy_table(df_ly, df_ty):
    """Build MTD/YTD YoY total-forms table: rows = metrics, columns = campuses."""
    ly_year = report_date.year - 1
    ly_mtd_start = f'{ly_year}-{report_date.month:02d}-01'
    ly_mtd_end   = f'{ly_year}-{report_date.month:02d}-{report_date.day:02d}'
    ly_ytd_start = f'{ly_year}-01-01'
    ly_ytd_end   = f'{ly_year}-{report_date.month:02d}-{report_date.day:02d}'

    def cnt(df, campus, start, end):
        if df.empty:
            return 0
        return int(((df['campus'] == campus) & df['date'].between(start, end)).sum())

    campuses = AMITY_CAMPUSES
    windows = [
        ('MTD Last Year', df_ly, ly_mtd_start, ly_mtd_end),
        ('MTD This Year', df_ty, MTD_START,     MTD_END),
        ('YTD Last Year', df_ly, ly_ytd_start,  ly_ytd_end),
        ('YTD This Year', df_ty, YTD_START,      FTD_DATE),
    ]
    data = {}
    for label, df, start, end in windows:
        data[label] = {c: cnt(df, c, start, end) for c in campuses}
        data[label]['Grand Total'] = sum(data[label].values())

    metrics = ['MTD Last Year', 'MTD This Year', 'YTD Last Year', 'YTD This Year']
    rows = campuses + ['Grand Total']
    # campuses as rows, metrics as columns
    df_out = pd.DataFrame(
        [{m: data[m][c] for m in metrics} for c in rows],
        index=rows
    ).reset_index()
    df_out.rename(columns={'index': 'Campus'}, inplace=True)
    return df_out


def _amity_yoy_html_section(df):
    """Render the Amity YoY total-forms table as an HTML panel.
    Rows = campuses, columns = metrics."""
    if df is None or df.empty:
        return '<p style="color:#888">No Amity YoY data available.</p>'

    metrics = ['MTD Last Year', 'MTD This Year', 'YTD Last Year', 'YTD This Year']

    header = ''.join(f'<th>{m}</th>' for m in metrics)
    tbody = ''
    for _, row in df.iterrows():
        campus = row['Campus']
        is_total = campus == 'Grand Total'
        cls = 'total-row' if is_total else ''
        sep = 'border-top:2px solid #c8c0b4;' if is_total else ''
        tbody += f'<tr class="{cls}" style="{sep}"><td class="college-name bold">{html.escape(campus)}</td>'
        for m in metrics:
            tbody += f'<td class="num">{row[m]}</td>'
        tbody += '</tr>\n'

    ly_year = report_date.year - 1
    ty_year = report_date.year
    return f'''
<div class="section-label"><span>Amity Total Forms — Campus YoY &middot; {ly_year} vs {ty_year} &middot; MTD &amp; YTD</span></div>
<div class="table-wrap">
<table>
<thead>
  <tr>
    <th style="width:16%;text-align:left">Campus</th>
    {header}
  </tr>
</thead>
<tbody>{tbody}</tbody>
</table>
</div>'''


# ─── AMITY ADMISSIONS YoY ────────────────────────────────────────────────────

_LAST_YEAR_ADMISSION_TAB = 'Last Year Admission'

_AMITY_ADM_SQL = """
SELECT DISTINCT ON (s.student_id, uc.course_id)
    uc.university_name AS campus_name,
    (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date AS adm_date
FROM students s
JOIN course_status_journeys csj ON s.student_id = csj.student_id
JOIN university_courses uc ON csj.course_id = uc.course_id
WHERE csj.course_status = 'Admission'
  AND csj.created_at >= '{ytd_start}'::date
  AND csj.created_at < '{ytd_end}'::date
ORDER BY s.student_id, uc.course_id, csj.created_at ASC
"""


def amity_get_admissions_last_year():
    """Read last year's Amity admissions from Google Sheet, exclude Drop/Lost."""
    service = get_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=AMITY_FORMS_SPREADSHEET_ID,
        range=f"'{_LAST_YEAR_ADMISSION_TAB}'!A1:Z"
    ).execute()
    rows = result.get('values', [])
    if not rows:
        return pd.DataFrame(columns=['campus', 'date'])
    h = rows[0]
    col_date   = next((i for i, v in enumerate(h) if v.strip().lower() == 'date'),   2)
    col_campus = next((i for i, v in enumerate(h) if v.strip().lower() == 'campus'), 5)
    records = []
    for row in rows[1:]:
        if len(row) <= max(col_date, col_campus):
            continue
        campus = _norm_amity_campus_sheet(row[col_campus])
        if not campus:
            continue
        raw_date = row[col_date].strip()
        dt = None
        for fmt in ('%d-%m-%Y', '%d/%m/%Y'):
            try:
                dt = datetime.strptime(raw_date, fmt).strftime('%Y-%m-%d')
                break
            except ValueError:
                continue
        if dt is None:
            continue
        records.append({'campus': campus, 'date': dt})
    print(f"  [Last Year Admission] {len(records)} admissions loaded")
    return pd.DataFrame(records) if records else pd.DataFrame(columns=['campus', 'date'])


async def amity_get_admissions_this_year():
    """Query AMITY DB for this year's admissions."""
    db = next(d for d in REGULAR_DB_CONFIGS if d['name'] == 'AMITY')
    ytd_start = f'{report_date.year}-01-01'
    ytd_end   = (report_date + timedelta(days=1)).strftime('%Y-%m-%d')
    sql = _AMITY_ADM_SQL.format(ytd_start=ytd_start, ytd_end=ytd_end)
    conn = await asyncpg.connect(host=db['host'], port=db['port'],
                                  database=db['database'], user=db['user'], password=db['password'])
    rows = await conn.fetch(sql)
    await conn.close()
    print(f"  [AMITY DB RAW] {len(rows)} rows fetched from DB")
    if rows:
        print(f"  [AMITY DB SAMPLE] campus={rows[0]['campus_name']!r}  adm_date={rows[0]['adm_date']!r}  type={type(rows[0]['adm_date']).__name__}")
    records = []
    skipped = 0
    for r in rows:
        campus = _norm_amity_campus_db(r['campus_name'])
        if campus:
            records.append({'campus': campus, 'date': str(r['adm_date'])})
        else:
            skipped += 1
            print(f"  [AMITY DB SKIP] unrecognised campus: {r['campus_name']!r}")
    print(f"  [AMITY DB] {len(records)} admissions loaded  ({skipped} skipped)")
    return pd.DataFrame(records) if records else pd.DataFrame(columns=['campus', 'date'])


def amity_build_admission_yoy_table(df_ly, df_ty):
    """Build MTD/YTD admissions table — no Growth rows."""
    ly_year      = report_date.year - 1
    ly_mtd_start = f'{ly_year}-{report_date.month:02d}-01'
    ly_mtd_end   = f'{ly_year}-{report_date.month:02d}-{report_date.day:02d}'
    ly_ytd_start = f'{ly_year}-01-01'
    ly_ytd_end   = f'{ly_year}-{report_date.month:02d}-{report_date.day:02d}'

    def cnt(df, campus, start, end):
        if df.empty:
            return 0
        return int(((df['campus'] == campus) & df['date'].between(start, end)).sum())

    campuses = AMITY_CAMPUSES
    windows = [
        ('MTD Last Year', df_ly, ly_mtd_start, ly_mtd_end),
        ('MTD This Year', df_ty, MTD_START,     MTD_END),
        ('YTD Last Year', df_ly, ly_ytd_start,  ly_ytd_end),
        ('YTD This Year', df_ty, YTD_START,      FTD_DATE),
    ]
    data = {}
    for label, df, start, end in windows:
        data[label] = {c: cnt(df, c, start, end) for c in campuses}
        data[label]['Grand Total'] = sum(data[label].values())

    metrics = ['MTD Last Year', 'MTD This Year', 'YTD Last Year', 'YTD This Year']
    rows    = campuses + ['Grand Total']
    df_out  = pd.DataFrame(
        [{m: data[m][c] for m in metrics} for c in rows],
        index=rows
    ).reset_index()
    df_out.rename(columns={'index': 'Campus'}, inplace=True)
    return df_out


def _amity_adm_yoy_html_section(df):
    """Render Amity admissions YoY table — no Growth column."""
    if df is None or df.empty:
        return '<p style="color:#888">No Amity admissions data available.</p>'

    metrics = ['MTD Last Year', 'MTD This Year', 'YTD Last Year', 'YTD This Year']
    header  = ''.join(f'<th>{m}</th>' for m in metrics)
    tbody   = ''
    for _, row in df.iterrows():
        campus   = row['Campus']
        is_total = campus == 'Grand Total'
        cls  = 'total-row' if is_total else ''
        sep  = 'border-top:2px solid #c8c0b4;' if is_total else ''
        tbody += f'<tr class="{cls}" style="{sep}"><td class="college-name bold">{html.escape(campus)}</td>'
        for m in metrics:
            tbody += f'<td class="num">{row[m]}</td>'
        tbody += '</tr>\n'

    ly_year = report_date.year - 1
    ty_year = report_date.year
    return f'''
<div class="section-label" style="margin-top:24px"><span>Amity Admissions — Campus YoY &middot; {ly_year} vs {ty_year} &middot; MTD &amp; YTD</span></div>
<div class="table-wrap">
<table>
<thead>
  <tr>
    <th style="width:16%;text-align:left">Campus</th>
    {header}
  </tr>
</thead>
<tbody>{tbody}</tbody>
</table>
</div>'''


def regular_prepare_data(df_adm, df_form):
    """Build all Regular report DataFrames in memory — no Excel involved."""
    cols = ["College", "YTD Ach", f"{MONTH_SHORT} Target", f"{MONTH_SHORT} Ach",
            f"{MONTH_SHORT} Ach %", "Week Target", "Week Ach", "Week Ach %",
            "FTD Target", "FTD Ach", "FTD Ach %"]
    sheets = {}
    for mode in ["Admissions Data", "Forms Data"]:
        df = df_adm if "Admissions" in mode else df_form
        df['date'] = df['created_at'].dt.strftime('%Y-%m-%d')
        rows = []
        totals = [0] * 7
        for col, tgts in COLLEGE_TARGETS.items():
            key_prefix = "admission" if "Admissions" in mode else "forms"
            apr_t = tgts.get(f"{key_prefix}_monthly", 0)
            w5_t  = math.ceil(apr_t / _weeks_in_month) if apr_t else 0
            ftd_t = math.ceil(apr_t / _days_in_month)  if apr_t else 0
            ytd_a = _count(df, col, start=YTD_START,       end=FTD_DATE)
            apr_a = _count(df, col, start=MTD_START,        end=MTD_END)
            w5_a  = _count(df, col, start=REG_WEEK_START,   end=REG_WEEK_END)
            ftd_a = _count(df, col, exact=FTD_DATE)
            rows.append([col, ytd_a, apr_t, apr_a, regular_pct(apr_a, apr_t),
                         w5_t, w5_a, regular_pct(w5_a, w5_t), ftd_t, ftd_a, regular_pct(ftd_a, ftd_t)])
            for i, v in enumerate([ytd_a, apr_t, apr_a, w5_t, w5_a, ftd_t, ftd_a]):
                totals[i] += v
        rows.append(["Total", totals[0], totals[1], totals[2], regular_pct(totals[2], totals[1]),
                     totals[3], totals[4], regular_pct(totals[4], totals[3]),
                     totals[5], totals[6], regular_pct(totals[6], totals[5])])
        sheets[mode] = pd.DataFrame(rows, columns=cols)

    # ── College Month-over-Month Comparison ──────────────────────────────────
    # df_adm and df_form both have 'date' set by the loop above
    def _chg_pct(chg, last):
        if last: return f"{(chg / last * 100):+.1f}%"
        return "+100%" if chg > 0 else "N/A"

    mom_rows = []
    for col in COLLEGE_TARGETS.keys():
        last_adm  = _count(df_adm,  col, start=LAST_MTD_START, end=LAST_MTD_END)
        this_adm  = _count(df_adm,  col, start=MTD_START,      end=MTD_END)
        last_form = _count(df_form, col, start=LAST_MTD_START, end=LAST_MTD_END)
        this_form = _count(df_form, col, start=MTD_START,      end=MTD_END)
        chg_adm   = this_adm  - last_adm
        chg_form  = this_form - last_form
        mom_rows.append([col, last_adm, this_adm, chg_adm,
                         last_form, this_form, chg_form])

    gt_la = sum(r[1] for r in mom_rows); gt_ta = sum(r[2] for r in mom_rows)
    gt_lf = sum(r[4] for r in mom_rows); gt_tf = sum(r[5] for r in mom_rows)
    gt_ca = gt_ta - gt_la;               gt_cf = gt_tf - gt_lf
    mom_rows.append(['Total', gt_la, gt_ta, gt_ca, gt_lf, gt_tf, gt_cf])
    sheets['College_MoM'] = pd.DataFrame(mom_rows, columns=[
        'College',
        f'Adm {LAST_MONTH_LABEL}', f'Adm {MONTH_LABEL}', 'Adm Δ',
        f'Form {LAST_MONTH_LABEL}', f'Form {MONTH_LABEL}', 'Form Δ',
    ])

    # ── College 12-Day Comparisons ────────────────────────────────────────────
    # Comparison A: First N days of prev month vs First N days of current month
    cmp_a_rows = []
    for col in COLLEGE_TARGETS.keys():
        a_prev_adm  = _count(df_adm,  col, start=LM_FIRST_N_START, end=LM_FIRST_N_END)
        a_curr_adm  = _count(df_adm,  col, start=FIRST12_START,    end=FIRST12_END)
        a_prev_form = _count(df_form, col, start=LM_FIRST_N_START, end=LM_FIRST_N_END)
        a_curr_form = _count(df_form, col, start=FIRST12_START,    end=FIRST12_END)
        cmp_a_rows.append([col, a_prev_adm, a_curr_adm, a_curr_adm - a_prev_adm, a_prev_form, a_curr_form, a_curr_form - a_prev_form])
    gt_a_pa = sum(r[1] for r in cmp_a_rows); gt_a_ca = sum(r[2] for r in cmp_a_rows)
    gt_a_pf = sum(r[4] for r in cmp_a_rows); gt_a_cf = sum(r[5] for r in cmp_a_rows)
    cmp_a_rows.append(['Total', gt_a_pa, gt_a_ca, gt_a_ca - gt_a_pa, gt_a_pf, gt_a_cf, gt_a_cf - gt_a_pf])
    sheets['College_12Day_A'] = pd.DataFrame(cmp_a_rows, columns=[
        'College',
        f'Adm First {_n_days} ({PREV_MONTH_LABEL})', f'Adm First {_n_days} ({MONTH_LABEL})', 'Adm Δ',
        f'Form First {_n_days} ({PREV_MONTH_LABEL})', f'Form First {_n_days} ({MONTH_LABEL})', 'Form Δ',
    ])

    # Comparison B: Last N days of prev month vs First N days of current month
    cmp_b_rows = []
    for col in COLLEGE_TARGETS.keys():
        b_prev_adm  = _count(df_adm,  col, start=LAST12_START,  end=LAST12_END)
        b_curr_adm  = _count(df_adm,  col, start=FIRST12_START, end=FIRST12_END)
        b_prev_form = _count(df_form, col, start=LAST12_START,  end=LAST12_END)
        b_curr_form = _count(df_form, col, start=FIRST12_START, end=FIRST12_END)
        cmp_b_rows.append([col, b_prev_adm, b_curr_adm, b_curr_adm - b_prev_adm, b_prev_form, b_curr_form, b_curr_form - b_prev_form])
    gt_b_pa = sum(r[1] for r in cmp_b_rows); gt_b_ca = sum(r[2] for r in cmp_b_rows)
    gt_b_pf = sum(r[4] for r in cmp_b_rows); gt_b_cf = sum(r[5] for r in cmp_b_rows)
    cmp_b_rows.append(['Total', gt_b_pa, gt_b_ca, gt_b_ca - gt_b_pa, gt_b_pf, gt_b_cf, gt_b_cf - gt_b_pf])
    sheets['College_12Day'] = pd.DataFrame(cmp_b_rows, columns=[
        'College',
        f'Adm Last {_n_days} ({PREV_MONTH_LABEL})', f'Adm First {_n_days} ({MONTH_LABEL})', 'Adm Δ',
        f'Form Last {_n_days} ({PREV_MONTH_LABEL})', f'Form First {_n_days} ({MONTH_LABEL})', 'Form Δ',
    ])

    print("✅ Regular LMS data prepared")
    return sheets


def _reg_col_mom_html_section(df):
    """Render College Wise Monthly Comparison table for the Regular report."""
    if df is None or df.empty:
        return '<p style="color:#888">No data available.</p>'

    name_map = {
        'Chandigarh University, Mohali':             'CU Mohali',
        'Lovely Professional University':            'LPU',
        'Chandigarh University, Lucknow':            'CU Lucknow',
        'Chandigarh Group of Colleges, Landran (CGC)': 'Landran',
        'Amity University (All Campuses)':           'Amity',
    }

    cols = list(df.columns)   # College, Adm LM, Adm TM, Adm Δ, Form LM, Form TM, Form Δ
    adm_lm_col  = cols[1]; adm_tm_col  = cols[2]
    form_lm_col = cols[4]; form_tm_col = cols[5]

    def chg_cls(val):
        try:
            v = int(val)
            return 'green' if v > 0 else ('red' if v < 0 else '')
        except:
            return ''

    header = (
        f'<th>{html.escape(adm_lm_col)}</th>'
        f'<th>{html.escape(adm_tm_col)}</th>'
        f'<th>Adm Δ</th>'
        f'<th>{html.escape(form_lm_col)}</th>'
        f'<th>{html.escape(form_tm_col)}</th>'
        f'<th>Form Δ</th>'
    )
    tbody = ''
    for _, row in df.iterrows():
        college  = str(row['College'])
        is_total = college.lower() == 'total'
        name     = '⭟ Total' if is_total else name_map.get(college, college)
        cls      = 'total-row' if is_total else ''
        sep      = 'border-top:2px solid #1a1a18;' if is_total else ''
        adm_chg_cls  = chg_cls(row.iloc[3])
        form_chg_cls = chg_cls(row.iloc[6])
        tbody += (
            f'<tr class="{cls}" style="{sep}">'
            f'<td class="college-name {"bold" if is_total else ""}">{html.escape(name)}</td>'
            f'<td class="num">{row.iloc[1]}</td>'
            f'<td class="num">{row.iloc[2]}</td>'
            f'<td class="num {adm_chg_cls}">{row.iloc[3]:+d}</td>'
            f'<td class="num">{row.iloc[4]}</td>'
            f'<td class="num">{row.iloc[5]}</td>'
            f'<td class="num {form_chg_cls}">{row.iloc[6]:+d}</td>'
            f'</tr>\n'
        )

    lm_label = adm_lm_col.replace('Adm ', '')
    tm_label = adm_tm_col.replace('Adm ', '')
    date_range = f'{LAST_MTD_START[8:]} – {LAST_MTD_END[8:]} {lm_label} vs {MTD_START[8:]} – {MTD_END[8:]} {tm_label}'
    return f'''
<div class="section-label"><span>College Wise Monthly Comparison &middot; {date_range}</span></div>
<div class="table-wrap">
<table>
<thead>
  <tr>
    <th rowspan="2" style="width:22%;vertical-align:middle;text-align:left;border-right:1px solid rgba(255,255,255,.12)">College</th>
    <th colspan="3" class="th-group">Admissions</th>
    <th colspan="3" class="th-group">Forms</th>
  </tr>
  <tr>{header}</tr>
</thead>
<tbody>{tbody}</tbody>
</table>
</div>'''


def _reg_col_12day_html_section(df_b, df_a=None):
    """Render two College N-Day Comparison tables:
       df_a — Comparison A: First N days of prev month vs First N days of current month
       df_b — Comparison B: Last N days of prev month  vs First N days of current month
    """
    name_map = {
        'Chandigarh University, Mohali':                'CU Mohali',
        'Lovely Professional University':               'LPU',
        'Chandigarh University, Lucknow':               'CU Lucknow',
        'Chandigarh Group of Colleges, Landran (CGC)':  'Landran',
        'Amity University (All Campuses)':              'Amity',
    }

    def chg_cls(val):
        try:
            v = int(val)
            return 'green' if v > 0 else ('red' if v < 0 else '')
        except:
            return ''

    def _render_table(df, label, date_range):
        if df is None or df.empty:
            return f'<p style="color:#888">No data for {label}.</p>'
        cols = list(df.columns)
        header = (
            f'<th>{html.escape(cols[1])}</th>'
            f'<th>{html.escape(cols[2])}</th>'
            f'<th>Adm Δ</th>'
            f'<th>{html.escape(cols[4])}</th>'
            f'<th>{html.escape(cols[5])}</th>'
            f'<th>Form Δ</th>'
        )
        tbody = ''
        for _, row in df.iterrows():
            college  = str(row['College'])
            is_total = college.lower() == 'total'
            name     = '⭟ Total' if is_total else name_map.get(college, college)
            cls      = 'total-row' if is_total else ''
            sep      = 'border-top:2px solid #1a1a18;' if is_total else ''
            tbody += (
                f'<tr class="{cls}" style="{sep}">'
                f'<td class="college-name {"bold" if is_total else ""}">{html.escape(name)}</td>'
                f'<td class="num">{row.iloc[1]}</td>'
                f'<td class="num">{row.iloc[2]}</td>'
                f'<td class="num {chg_cls(row.iloc[3])}">{row.iloc[3]:+d}</td>'
                f'<td class="num">{row.iloc[4]}</td>'
                f'<td class="num">{row.iloc[5]}</td>'
                f'<td class="num {chg_cls(row.iloc[6])}">{row.iloc[6]:+d}</td>'
                f'</tr>\n'
            )
        return f'''
<div class="section-label" style="margin-top:20px"><span>{html.escape(label)} &middot; {date_range}</span></div>
<div class="table-wrap">
<table>
<thead>
  <tr>
    <th rowspan="2" style="width:22%;vertical-align:middle;text-align:left;border-right:1px solid rgba(255,255,255,.12)">College</th>
    <th colspan="3" class="th-group">Admissions</th>
    <th colspan="3" class="th-group">Forms</th>
  </tr>
  <tr>{header}</tr>
</thead>
<tbody>{tbody}</tbody>
</table>
</div>'''

    out = ''
    if df_a is not None and not df_a.empty:
        range_a = f'{LM_FIRST_N_START[8:]} – {LM_FIRST_N_END[8:]} {PREV_MONTH_LABEL} vs {FIRST12_START[8:]} – {FIRST12_END[8:]} {MONTH_LABEL}'
        out += _render_table(df_a, f'First {_n_days} Days — {PREV_MONTH_LABEL} vs {MONTH_LABEL}', range_a)

    if df_b is not None and not df_b.empty:
        range_b = f'{LAST12_START[8:]} – {LAST12_END[8:]} {PREV_MONTH_LABEL} vs {FIRST12_START[8:]} – {FIRST12_END[8:]} {MONTH_LABEL}'
        out += _render_table(df_b, f'Last {_n_days} {PREV_MONTH_LABEL} vs First {_n_days} {MONTH_LABEL}', range_b)

    return out or '<p style="color:#888">No data available.</p>'


def _reg_mom_breakdown_html_section(df_mom, dim_label, section_title, df_12day=None):
    """Render MOM + optional 12-day breakdown tables (UTM Campaign or Counsellor)."""

    def chg_cls(val):
        try:
            v = int(val)
            return 'green' if v > 0 else ('red' if v < 0 else '')
        except:
            return ''

    def _render_table(df, note_html, label_col):
        if df is None or df.empty:
            return ''
        cols = list(df.columns)
        header = (
            f'<th>{html.escape(cols[1])}</th>'
            f'<th>{html.escape(cols[2])}</th>'
            f'<th>Adm Δ</th>'
            f'<th>{html.escape(cols[4])}</th>'
            f'<th>{html.escape(cols[5])}</th>'
            f'<th>Form Δ</th>'
        )
        tbody = ''
        for _, row in df.iterrows():
            name     = str(row[cols[0]])
            is_total = name.lower() == 'total'
            cls      = 'total-row' if is_total else ''
            sep      = 'border-top:2px solid #1a1a18;' if is_total else ''
            adm_chg  = row.iloc[3]; form_chg = row.iloc[6]
            adm_c    = chg_cls(adm_chg); form_c = chg_cls(form_chg)
            adm_sign  = '+' if isinstance(adm_chg, (int, float)) and adm_chg > 0 else ''
            form_sign = '+' if isinstance(form_chg, (int, float)) and form_chg > 0 else ''
            tbody += (
                f'<tr class="{cls}" style="{sep}">'
                f'<td class="college-name {"bold" if is_total else ""}">{html.escape(name)}</td>'
                f'<td class="num">{int(row.iloc[1])}</td>'
                f'<td class="num">{int(row.iloc[2])}</td>'
                f'<td class="num" style="color:var(--{adm_c if adm_c else "ink"},inherit);font-weight:{"700" if adm_c else "400"}">'
                f'{adm_sign}{int(adm_chg)}</td>'
                f'<td class="num">{int(row.iloc[4])}</td>'
                f'<td class="num">{int(row.iloc[5])}</td>'
                f'<td class="num" style="color:var(--{form_c if form_c else "ink"},inherit);font-weight:{"700" if form_c else "400"}">'
                f'{form_sign}{int(form_chg)}</td>'
                f'</tr>\n'
            )
        return f'''{note_html}
<div class="table-wrap">
<table>
<thead>
  <tr>
    <th rowspan="2" style="width:26%;vertical-align:middle;text-align:left;border-right:1px solid rgba(255,255,255,.12)">{html.escape(label_col)}</th>
    <th colspan="3" class="th-group">Admissions</th>
    <th colspan="3" class="th-group">Forms</th>
  </tr>
  <tr>{header}</tr>
</thead>
<tbody>{tbody}</tbody>
</table>
</div>'''

    lm_range = f'{_lm_first.day} {_lm_first.strftime("%b")} – {_lm_same_day.day} {_lm_same_day.strftime("%b %Y")}'
    tm_range = f'{report_date.replace(day=1).day} {report_date.strftime("%b")} – {report_date.day} {report_date.strftime("%b %Y")}'
    _prev_last_day  = report_date.replace(day=1) - timedelta(days=1)
    _days_in_lm_il  = calendar.monthrange(_prev_last_day.year, _prev_last_day.month)[1]
    _b_n_inline     = min(report_date.day, _days_in_lm_il)
    last12_start_dt = _prev_last_day - timedelta(days=_b_n_inline - 1)
    l12_range = f'{last12_start_dt.day} – {_prev_last_day.day} {_prev_last_day.strftime("%b %Y")}'
    f12_range = f'01 – {report_date.day:02d} {report_date.strftime("%b %Y")}'

    def _note(text):
        return (f'<div style="font-size:11px;color:#8896a8;margin-bottom:14px;padding:6px 10px;'
                f'background:#eef2f7;border-left:3px solid #0097a7;border-radius:3px">{text}</div>')

    mom_note  = _note(f'Same day window &nbsp;·&nbsp; <strong>{lm_range}</strong> &nbsp;vs&nbsp; <strong>{tm_range}</strong>')
    d12_note  = _note(f'Last 12 days vs First 12 days &nbsp;·&nbsp; <strong>{l12_range}</strong> &nbsp;vs&nbsp; <strong>{f12_range}</strong>')

    out = f'<div class="section-label"><span>{html.escape(section_title)} — MTD Comparison</span></div>\n'
    out += _render_table(df_mom, mom_note, dim_label)

    if df_12day is not None and not df_12day.empty:
        out += f'\n<div class="section-label" style="margin-top:24px"><span>{html.escape(section_title)} — 12-Day Comparison</span></div>\n'
        out += _render_table(df_12day, d12_note, dim_label)

    return out


def regular_generate_html(sheets, amity_yoy_df=None, amity_adm_df=None):
    """Generate Regular LMS HTML dashboard directly from in-memory DataFrames."""
    out_path = os.path.join(OUTPUT_DIR, f'Degreefyd_Regular_LMS_HTML_Report_{RUN_STAMP}.html')

    adm = sheets['Admissions Data']
    forms = sheets['Forms Data']
    col_mom     = sheets.get('College_MoM',     pd.DataFrame())
    col_12day   = sheets.get('College_12Day',   pd.DataFrame())
    col_12day_a = sheets.get('College_12Day_A', pd.DataFrame())

    def esc(v):
        return html.escape(str(v))

    def n(v):
        try:
            if pd.isna(v):
                return '\u2014'
            return f'{int(float(v)):,}'
        except:
            return esc(v)

    def pv(x):
        text = str(x)
        try:
            if '/' in text:
                a, t = text.split('/', 1)
                a = float(a.replace(',', '').strip() or 0)
                t = float(t.replace(',', '').strip() or 0)
                return (a / t * 100) if t else 0.0
            return float(text.replace('%', ''))
        except:
            return 0.0

    def pc(p):
        p = pv(p)
        if p >= 100:
            return 'green'
        if p >= 70:
            return 'amber'
        return 'red'

    def pill(p):
        return f'<span class=\"pct {pc(p)}\">{pv(p):.1f}%</span>'

    def row_html(r):
        college = str(r['College'])
        grand = college.lower() == 'total'
        name_map = {'Chandigarh University, Mohali': 'CU', 'Lovely Professional University': 'LPU',
                    'Chandigarh University, Lucknow': 'CU Lucknow',
                    'Chandigarh Group of Colleges, Landran (CGC)': 'Landran',
                    'Amity University (All Campuses)': 'Amity'}
        name = '\u2b5f Total' if grand else name_map.get(college, college)
        cls = 'total-row' if grand else ''
        vals = [r.iloc[i] for i in range(1, 11)]
        return f'<tr class=\"{cls}\"><td class=\"college-name {"bold" if grand else ""}\">{esc(name)}</td>' + \
            ''.join(f'<td class="num">{n(vals[i])}</td>' for i in [0, 1, 2]) + \
            f'<td>{pill(vals[3])}</td>' + \
            ''.join(f'<td class="num">{n(vals[i])}</td>' for i in [4, 5]) + \
            f'<td>{pill(vals[6])}</td>' + \
            f'<td class="num">{n(vals[7])}</td><td class="num">{n(vals[8])}</td>' + \
            f'<td>{pill(vals[9])}</td></tr>'

    def rows(df):
        return '\n'.join(row_html(r) for _, r in df.iterrows())

    admt = adm.iloc[-1]
    formt = forms.iloc[-1]

    _CSS_BASE_REGULAR = r'''
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--white:#fff;--off:#f0ede8;--border:#d8d2c8;--border-dark:#b0a898;--ink:#111110;--ink-mid:#444440;--ink-light:#7a7570;--gold:#c8a84b;--gold-light:#f0e4c0;--green:#1e6b3c;--green-bg:#d4eddf;--amber:#a05e10;--amber-bg:#faecd4;--red:#b02020;--red-bg:#fad4d4;--blue:#1a4a8a;--blue-bg:#e8eef7;--radius:3px}
body{background:#f4f2ee;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:var(--ink);min-height:100vh}
#tab-admissions,#tab-forms,#tab-amity-yoy,#tab-college-mom,#tab-college-12day{display:none}
.shell{max-width:1400px;margin:0 auto;padding:0 28px 40px}
.header{padding:28px 0 20px;border-bottom:3px solid var(--ink);margin-bottom:24px}
.header-top{display:flex;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;gap:8px}
.brand{font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--ink-light);margin-bottom:6px}
.title{font-size:clamp(22px,5vw,32px);font-weight:700;letter-spacing:-.01em;line-height:1.1;color:var(--ink)}
.header-meta{text-align:right}
.badge{display:inline-block;background:var(--gold);color:#fff;font-size:9px;letter-spacing:.14em;text-transform:uppercase;padding:3px 8px;border-radius:var(--radius);margin-bottom:4px}
.date{font-size:12px;color:var(--ink-light);letter-spacing:.04em}
.tabs{display:flex;gap:0;margin-bottom:24px;border:2px solid var(--border-dark);border-radius:var(--radius);overflow:hidden;position:sticky;top:0;z-index:100;background:#f4f2ee}
.tab-label{flex:1;display:flex;align-items:center;justify-content:center;gap:8px;padding:12px 16px;cursor:pointer;font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-mid);background:var(--off);user-select:none;font-weight:600}
.tab-label:not(:last-child){border-right:1.5px solid var(--border-dark)}
#tab-admissions:checked~.shell .tab-label[for=tab-admissions],#tab-forms:checked~.shell .tab-label[for=tab-forms],#tab-amity-forms:checked~.shell .tab-label[for=tab-amity-forms],#tab-amity-adm:checked~.shell .tab-label[for=tab-amity-adm],#tab-college-mom:checked~.shell .tab-label[for=tab-college-mom],#tab-college-12day:checked~.shell .tab-label[for=tab-college-12day]{background:var(--ink);color:var(--white)}
.panel{display:none}
#tab-admissions:checked~.shell #panel-admissions,#tab-forms:checked~.shell #panel-forms,#tab-amity-forms:checked~.shell #panel-amity-forms,#tab-amity-adm:checked~.shell #panel-amity-adm,#tab-college-mom:checked~.shell #panel-college-mom,#tab-college-12day:checked~.shell #panel-college-12day{display:block}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin-bottom:28px}
.kpi{background:var(--white);border:1px solid var(--border);border-left:4px solid var(--ink);border-radius:var(--radius);padding:12px 14px}
.kpi-label{font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--ink-light);margin-bottom:3px}
.kpi-value{font-size:22px;font-weight:700;line-height:1.1}
.kpi-value.green{color:var(--green)}.kpi-value.amber{color:var(--amber)}.kpi-value.red{color:var(--red)}
.kpi-sub{font-size:11px;color:var(--ink-mid);margin-top:2px}
.section-label{font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--ink);margin-bottom:12px;padding-bottom:6px;border-bottom:2px solid var(--ink)}
.table-wrap{overflow-x:auto;border-radius:4px;border:2px solid var(--border-dark)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:#1a1a18;color:#ffffff;padding:10px 8px;text-align:center;font-weight:700;font-size:11px;letter-spacing:.05em;border-right:1px solid rgba(255,255,255,.15)}
th.th-group{background:#2e2e2c;color:rgba(255,255,255,.9);font-size:10.5px}
th:last-child{border-right:none}
td{padding:8px 8px;text-align:center;border-bottom:1px solid var(--border);font-variant-numeric:tabular-nums;font-size:13px}
tbody tr:nth-child(even) td{background:#f7f5f1}
tbody tr:nth-child(odd) td{background:#ffffff}
tr:last-child td{border-bottom:none}
tbody tr:hover td{background:#eef0f8}
.college-name{text-align:left;font-weight:600;padding-left:12px}
.bold{font-weight:700}
.num{font-variant-numeric:tabular-nums;font-weight:500}
.pct{font-weight:700;font-size:11px;padding:3px 8px;border-radius:3px;display:inline-block}
.pct.green{background:var(--green-bg);color:var(--green)}
.pct.amber{background:var(--amber-bg);color:var(--amber)}
.pct.red{background:var(--red-bg);color:var(--red)}
.total-row td{background:#e0ddd8!important;font-weight:700;border-top:3px solid var(--ink)}
.legend{display:flex;gap:16px;margin-top:14px;font-size:11px;color:var(--ink-mid)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}
.dot.green{background:var(--green)}.dot.amber{background:var(--amber)}.dot.red{background:var(--red)}
.footer{margin-top:28px;padding:16px 0 8px;border-top:1px solid var(--border);font-size:11px;color:var(--ink-light);text-align:center}
.footer-brand{font-weight:600;color:var(--ink-mid);margin-top:4px}
@media(max-width:600px){.kpis{grid-template-columns:1fr 1fr}.kpi-value{font-size:22px}table{font-size:11.5px}.header-top{flex-wrap:nowrap;align-items:flex-start}.header-meta{flex-shrink:0;min-width:fit-content}}
'''

    _CSS_COLORFUL_REGULAR = r'''
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --navy:#0d1b2a;--navy-mid:#1b2e45;
  --teal:#0097a7;--teal-light:#e0f7fa;
  --white:#fff;--surface:#f7f9fc;--surface2:#eef2f7;
  --border:#dce3ed;--border-strong:#b8c4d4;
  --ink:#0d1b2a;--ink-mid:#4a5568;--ink-light:#8896a8;
  --green:#1a7f4b;--green-bg:#e3f5ec;
  --amber:#b45309;--amber-bg:#fef3c7;
  --red:#b91c1c;--red-bg:#fee2e2;
  --blue:#1d4ed8;--blue-bg:#eff6ff;
  --radius:5px}
body{background:var(--surface);font-family:'Segoe UI',-apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif;color:var(--ink);min-height:100vh}
#tab-admissions,#tab-forms,#tab-amity-yoy,#tab-college-mom,#tab-college-12day{display:none}
.shell{max-width:1400px;margin:0 auto;padding:0 28px 44px}
.header{background:var(--navy);padding:26px 28px 22px;border-radius:8px;margin-bottom:20px;border-left:5px solid var(--teal)}
.header-top{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}
.brand{font-size:9px;letter-spacing:.22em;text-transform:uppercase;color:var(--teal);margin-bottom:5px;font-weight:600}
.title{font-size:clamp(19px,3.5vw,26px);font-weight:700;line-height:1.2;color:#fff;letter-spacing:-.01em}
.header-meta{text-align:right}
.badge{display:inline-block;background:var(--teal);color:#fff;font-size:9px;letter-spacing:.12em;text-transform:uppercase;padding:4px 12px;border-radius:20px;margin-bottom:5px;font-weight:600}
.date{font-size:11px;color:rgba(255,255,255,.55);letter-spacing:.04em}
.tabs{display:flex;gap:8px;margin-bottom:20px;position:sticky;top:0;z-index:100;background:var(--surface);padding:10px 0}
.tab-label{flex:1;display:flex;align-items:center;justify-content:center;gap:6px;padding:10px 12px;cursor:pointer;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-mid);background:var(--white);user-select:none;font-weight:600;border-radius:var(--radius);border:1.5px solid var(--border-strong)}
#tab-admissions:checked~.shell .tab-label[for=tab-admissions]{background:var(--navy);color:#fff;border-color:var(--navy)}
#tab-forms:checked~.shell .tab-label[for=tab-forms]{background:var(--teal);color:#fff;border-color:var(--teal)}
#tab-amity-forms:checked~.shell .tab-label[for=tab-amity-forms]{background:var(--green);color:#fff;border-color:var(--green)}
#tab-amity-adm:checked~.shell .tab-label[for=tab-amity-adm]{background:var(--amber);color:#fff;border-color:var(--amber)}
#tab-college-mom:checked~.shell .tab-label[for=tab-college-mom]{background:var(--blue);color:#fff;border-color:var(--blue)}
#tab-college-12day:checked~.shell .tab-label[for=tab-college-12day]{background:#6d28d9;color:#fff;border-color:#6d28d9}
.panel{display:none}
#tab-admissions:checked~.shell #panel-admissions,#tab-forms:checked~.shell #panel-forms,#tab-amity-forms:checked~.shell #panel-amity-forms,#tab-amity-adm:checked~.shell #panel-amity-adm,#tab-college-mom:checked~.shell #panel-college-mom,#tab-college-12day:checked~.shell #panel-college-12day{display:block}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin-bottom:26px}
.kpi{background:var(--white);border:1px solid var(--border);border-radius:var(--radius);padding:16px 18px;border-top:3px solid var(--teal)}
.kpi:nth-child(2){border-top-color:var(--green)}.kpi:nth-child(3){border-top-color:var(--amber)}
.kpi-label{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--ink-light);margin-bottom:4px;font-weight:600}
.kpi-value{font-size:22px;font-weight:700;line-height:1.1;color:var(--ink)}
.kpi-value.green{color:var(--green)}.kpi-value.amber{color:var(--amber)}.kpi-value.red{color:var(--red)}
.kpi-sub{font-size:11px;color:var(--ink-light);margin-top:3px}
.section-label{font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--navy-mid);margin:20px 0 12px;padding:0 0 8px;border-bottom:2px solid var(--teal);display:flex;align-items:center;gap:8px}
.section-label::before{content:'';display:inline-block;width:4px;height:14px;background:var(--teal);border-radius:2px}
.table-wrap{overflow-x:auto;border-radius:6px;border:1px solid var(--border-strong);background:var(--white)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:var(--navy);color:#fff;padding:11px 10px;text-align:center;font-weight:600;font-size:11px;letter-spacing:.05em;border-right:1px solid rgba(255,255,255,.1)}
th.th-group{background:var(--teal);color:#fff;font-size:10.5px;font-weight:700;letter-spacing:.06em}
th:last-child{border-right:none}
td{padding:9px 10px;text-align:center;border-bottom:1px solid var(--border);font-variant-numeric:tabular-nums;font-size:13px;color:var(--ink)}
tbody tr:nth-child(even) td{background:#f9fbff}
tbody tr:nth-child(odd) td{background:var(--white)}
tr:last-child td{border-bottom:none}
tbody tr:hover td{background:#eef4ff}
.college-name{text-align:left;font-weight:600;padding-left:14px}
.bold{font-weight:700}.num{font-variant-numeric:tabular-nums;font-weight:500}
.pct{font-weight:700;font-size:11px;padding:3px 9px;border-radius:4px;display:inline-block;letter-spacing:.02em}
.pct.green{background:var(--green-bg);color:var(--green)}.pct.amber{background:var(--amber-bg);color:var(--amber)}.pct.red{background:var(--red-bg);color:var(--red)}
.total-row td{background:var(--navy-mid)!important;color:#fff!important;font-weight:700;border-top:2px solid var(--navy)}
.legend{display:flex;gap:18px;margin-top:14px;font-size:11px;color:var(--ink-light);flex-wrap:wrap}
.dot{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:5px}
.dot.green{background:var(--green)}.dot.amber{background:var(--amber)}.dot.red{background:var(--red)}
.footer{margin-top:28px;padding:16px 0 8px;border-top:1px solid var(--border);font-size:11px;color:var(--ink-light);text-align:center}
.footer-brand{font-weight:600;color:var(--ink-mid);margin-top:4px}
@media(max-width:600px){.kpis{grid-template-columns:1fr 1fr}.kpi-value{font-size:22px}table{font-size:11.5px}}
'''

    _CSS_SIDEBAR_REGULAR = '.layout{display:flex;gap:16px;align-items:flex-start}.sidebar{width:155px;flex-shrink:0;position:sticky;top:10px;display:flex;flex-direction:column;gap:6px}.sidebar .tab-label{justify-content:flex-start;padding:10px 12px;white-space:normal;text-align:left}.content{flex:1;min-width:0}'
    CSS = (_CSS_COLORFUL_REGULAR if COLORFUL_MODE else _CSS_BASE_REGULAR) + (_CSS_SIDEBAR_REGULAR if SIDEBAR_NAV else '')

    _reg_tabs_horizontal = '<div class="tabs"><label class="tab-label" for="tab-admissions">&#x1f393; Admissions</label><label class="tab-label" for="tab-forms">&#x1f4cb; Forms</label><label class="tab-label" for="tab-amity-forms">&#x1f4ca; Amity Forms YoY</label><label class="tab-label" for="tab-amity-adm">&#x1f4ca; Amity Adm YoY</label><label class="tab-label" for="tab-college-mom">&#x1f4c5; College MoM</label><label class="tab-label" for="tab-college-12day">&#x1f4c6; 12-Day Compare</label></div>'
    _reg_tabs_sidebar    = '<div class="layout"><nav class="sidebar"><label class="tab-label" for="tab-admissions">&#x1f393; Admissions</label><label class="tab-label" for="tab-forms">&#x1f4cb; Forms</label><label class="tab-label" for="tab-amity-forms">&#x1f4ca; Amity Forms YoY</label><label class="tab-label" for="tab-amity-adm">&#x1f4ca; Amity Adm YoY</label><label class="tab-label" for="tab-college-mom">&#x1f4c5; College MoM</label><label class="tab-label" for="tab-college-12day">&#x1f4c6; 12-Day Compare</label></nav><div class="content">'
    _reg_tabs_block = _reg_tabs_sidebar if SIDEBAR_NAV else _reg_tabs_horizontal
    _reg_close = '</div></div></div>' if SIDEBAR_NAV else '</div>'   # .content + .layout + .shell  OR just .shell

    html_doc = f'''<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Regular Admissions Dashboard</title><style>{CSS}</style></head><body>
<input type="radio" name="view" id="tab-admissions" checked>
<input type="radio" name="view" id="tab-forms">
<input type="radio" name="view" id="tab-amity-forms">
<input type="radio" name="view" id="tab-amity-adm">
<input type="radio" name="view" id="tab-college-mom">
<input type="radio" name="view" id="tab-college-12day">
<div class="shell">
<header class="header"><div class="header-top"><div><div class="brand">Performance Intelligence &middot; Regular</div><h1 class="title">Regular Admissions &amp; Forms Tracker</h1></div><div class="header-meta"><div class="badge">Live Report</div><div class="date">{MONTH_LABEL} &middot; FTD {report_date.strftime('%d %b')}</div></div></div></header>
{_reg_tabs_block}

<section class="panel" id="panel-admissions">
<div class="kpis">
<div class="kpi"><div class="kpi-label">YTD Total</div><div class="kpi-value">{n(admt['YTD Ach'])}</div><div class="kpi-sub">Admissions</div></div>
<div class="kpi"><div class="kpi-label">{MONTH_SHORT} Ach %</div><div class="kpi-value {pc(admt.iloc[4])}">{pv(admt.iloc[4]):.1f}%</div><div class="kpi-ratio" style="font-size:11px;opacity:0.8">{admt.iloc[3]}/{admt.iloc[2]}</div></div>
<div class="kpi"><div class="kpi-label">Week Ach %</div><div class="kpi-value {pc(admt.iloc[7])}">{pv(admt.iloc[7]):.1f}%</div><div class="kpi-ratio" style="font-size:11px;opacity:0.8">{admt.iloc[6]}/{admt.iloc[5]}</div></div>
</div>
<div class="section-label"><span>College-wise Breakdown</span></div>
<div class="table-wrap"><table><thead><tr><th rowspan="2" style="width:30%;vertical-align:middle;border-right:1px solid rgba(255,255,255,.12)">College</th><th rowspan="2" style="vertical-align:middle;color:rgba(255,255,255,.9);border-right:1px solid rgba(255,255,255,.12)">YTD</th><th colspan="3" class="th-group">{MONTH_SHORT}</th><th colspan="3" class="th-group">Week</th><th colspan="3" class="th-group">FTD</th></tr><tr><th>Target</th><th>Ach</th><th>Ach %</th><th>Target</th><th>Ach</th><th>Ach %</th><th>Target</th><th>Ach</th><th>Ach %</th></tr></thead><tbody>{rows(adm)}</tbody></table></div>
</section>

<section class="panel" id="panel-forms">
<div class="kpis">
<div class="kpi"><div class="kpi-label">YTD Total</div><div class="kpi-value">{n(formt['YTD Ach'])}</div><div class="kpi-sub">Forms</div></div>
<div class="kpi"><div class="kpi-label">{MONTH_SHORT} Ach %</div><div class="kpi-value {pc(formt.iloc[4])}">{pv(formt.iloc[4]):.1f}%</div><div class="kpi-ratio" style="font-size:11px;opacity:0.8">{formt.iloc[3]}/{formt.iloc[2]}</div></div>
<div class="kpi"><div class="kpi-label">Week Ach %</div><div class="kpi-value {pc(formt.iloc[7])}">{pv(formt.iloc[7]):.1f}%</div><div class="kpi-ratio" style="font-size:11px;opacity:0.8">{formt.iloc[6]}/{formt.iloc[5]}</div></div>
</div>
<div class="section-label"><span>College-wise Breakdown</span></div>
<div class="table-wrap"><table><thead><tr><th rowspan="2" style="width:30%;vertical-align:middle;border-right:1px solid rgba(255,255,255,.12)">College</th><th rowspan="2" style="vertical-align:middle;color:rgba(255,255,255,.9);border-right:1px solid rgba(255,255,255,.12)">YTD</th><th colspan="3" class="th-group">{MONTH_SHORT}</th><th colspan="3" class="th-group">Week</th><th colspan="3" class="th-group">FTD</th></tr><tr><th>Target</th><th>Ach</th><th>Ach %</th><th>Target</th><th>Ach</th><th>Ach %</th><th>Target</th><th>Ach</th><th>Ach %</th></tr></thead><tbody>{rows(forms)}</tbody></table></div>
</section>

<section class="panel" id="panel-amity-forms">
{_amity_yoy_html_section(amity_yoy_df)}
</section>

<section class="panel" id="panel-amity-adm">
{_amity_adm_yoy_html_section(amity_adm_df)}
</section>

<section class="panel" id="panel-college-mom">
{_reg_col_mom_html_section(col_mom)}
</section>

<section class="panel" id="panel-college-12day">
{_reg_col_12day_html_section(col_12day, df_a=col_12day_a)}
</section>

{_reg_close}</body></html>'''

    with open(out_path, 'w', encoding='utf-8-sig') as f:
        f.write(html_doc)
    print(f"✅ Regular LMS HTML: {out_path}")
    adm_total = admt['YTD Ach'] if 'YTD Ach' in admt.index else 0
    forms_total = formt['YTD Ach'] if 'YTD Ach' in formt.index else 0
    summary = f"Adm YTD: {n(adm_total)}, Forms YTD: {n(forms_total)}, FTD Adm: {n(admt.iloc[9])}"
    return out_path, summary


# ═══════════════════════════════════════════════════════════════════════════════
# PART 3 — SEND TO WHATSAPP
# ═══════════════════════════════════════════════════════════════════════════════

def _save_fallback(file_path):
    """Copy file to LOCAL_FALLBACK_DIR when WHAPI delivery fails."""
    try:
        dest = os.path.join(LOCAL_FALLBACK_DIR, os.path.basename(file_path))
        shutil.copy2(file_path, dest)
        print(f"  💾 Saved locally: {dest}")
    except Exception as copy_err:
        print(f"  ⚠️  Could not copy to fallback dir: {copy_err}")


def send_via_whapi(file_path, caption, group_id=None):
    """Send a file to a WhatsApp group via WHAPI (best-effort).
    group_id defaults to WHATSAPP_GROUP_ONLINE when not provided.
    On any failure the file is copied to LOCAL_FALLBACK_DIR."""
    if not WHAPI_TOKEN:
        print(f"  ⚠️  WHAPI_TOKEN_PAID not set — file already at: {file_path}")
        _save_fallback(file_path)
        return False

    if group_id is None:
        group_id = WHATSAPP_GROUP_ONLINE

    filename = os.path.basename(file_path)
    ext = os.path.splitext(filename)[1].lower()

    with open(file_path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode('utf-8')

    mime_map = {
        '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        '.xls': 'application/vnd.ms-excel',
        '.html': 'text/html',
        '.htm': 'text/html',
        '.pdf': 'application/pdf',
        '.csv': 'text/csv',
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
    }
    mime = mime_map.get(ext, 'application/octet-stream')
    media_data = f'data:{mime};name={filename};base64,{b64}'
    is_image = ext in ('.png', '.jpg', '.jpeg')
    endpoint = 'messages/image' if is_image else 'messages/document'

    payload = {'to': group_id, 'media': media_data, 'caption': caption}
    headers = {'accept': 'application/json', 'authorization': f'Bearer {WHAPI_TOKEN}',
               'content-type': 'application/json'}

    for attempt in range(2):
        try:
            r = requests.post(f'https://gate.whapi.cloud/{endpoint}',
                              headers=headers, json=payload, timeout=20)
            if 200 <= r.status_code < 300:
                print(f"  ✅ WHAPI sent: {filename}")
                return True
            print(f"  ⚠️  WHAPI HTTP {r.status_code} for {filename}: {r.text[:150]}")
            _save_fallback(file_path)
            return False
        except requests.exceptions.Timeout:
            print(f"  ⚠️  WHAPI timeout for {filename}")
            _save_fallback(file_path)
            return False
        except Exception as e:
            print(f"  ⚠️  WHAPI error for {filename}: {e}")
            if attempt == 0:
                time.sleep(3)
    _save_fallback(file_path)
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# SCREENSHOT — per-tab
# ═══════════════════════════════════════════════════════════════════════════════

async def screenshot_html_tabs(html_path, tab_ids, png_paths, viewport_width=1600, sidebar_nav=False):
    """Screenshot a tabbed HTML page once per tab by clicking each tab label.
    tab_ids: list of the `for` attr values on each <label> (e.g. ['t1','t2',...]).
    sidebar_nav: if True, screenshots the .content div (tabs excluded) instead of .shell.
    Returns list of booleans indicating success for each tab."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("  ⚠️  playwright not installed — run: pip install playwright && playwright install chromium")
        return [False] * len(tab_ids)

    results = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=['--no-sandbox', '--disable-setuid-sandbox'])
            for tab_id, png_path in zip(tab_ids, png_paths):
                try:
                    page = await browser.new_page(
                        viewport={'width': viewport_width, 'height': 900},
                        device_scale_factor=2,
                    )
                    await page.goto(f'file:///{os.path.abspath(html_path)}', wait_until='networkidle', timeout=30000)
                    await page.click(f'label[for="{tab_id}"]')
                    await page.wait_for_timeout(400)
                    _locator = '.content' if sidebar_nav else '.shell'
                    await page.locator(_locator).screenshot(path=png_path)
                    await page.close()
                    print(f"  ✅ Screenshot saved: {os.path.basename(png_path)}")
                    results.append(True)
                except Exception as tab_err:
                    print(f"  ⚠️  Screenshot failed for tab '{tab_id}': {tab_err}")
                    results.append(False)
            await browser.close()
    except Exception as e:
        print(f"  ⚠️  Browser launch failed: {e}")
        while len(results) < len(tab_ids):
            results.append(False)
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

async def main():
    print("=" * 60)
    print("📊 UNIFIED LMS REPORTS — ONLINE + REGULAR")
    print("=" * 60)
    print(f"   FTD: {FTD_DATE}")
    print(f"   MTD: {MTD_START} → {MTD_END}")
    print(f"   Regular Week: {REG_WEEK_START} → {REG_WEEK_END}")
    print()

    # ── STEP 1: Online LMS ─────────────────────────────────────────────────
    print("─── STEP 1/3: Online LMS Report ────────────────────────────────────")
    online_summary = None
    try:
        print("  [1a] Fetching online LMS data from DB...")
        df_couns, df_couns_fee, df_couns_adm, df_college, df_last_activity_raw, df_couns_all, df_last_mtd_adm, df_college_last_mtd, df_last_mtd_fee, df_college_mtd_fee, df_college_last_mtd_fee = await online_get_data()
        print("  [1b] Preparing online LMS data...")
        online_sheets = online_prepare_data(df_couns, df_couns_fee, df_couns_adm, df_college, df_last_activity_raw, df_couns_all, df_last_mtd_adm, df_college_last_mtd, df_last_mtd_fee, df_college_mtd_fee, df_college_last_mtd_fee)
        print("  [1c] Generating online LMS HTML report...")
        online_html, online_summary = online_generate_html(online_sheets)
        print("  ✅ STEP 1 complete — Online LMS report generated")
        print()
    except Exception as e:
        print(f"  ❌ STEP 1 FAILED: {e}")
        import traceback
        traceback.print_exc(file=sys.stdout)
        online_html = None

    # ── STEP 1d: Screenshot Online tabs ───────────────────────────────────
    online_pngs = []
    if online_html:
        print("  [1d] Taking screenshots of Online LMS tabs...")
        if LAST_ACTIVITY_ONLY:
            _online_tab_ids   = ['t6']
            _online_tab_names = ['Last_Activity']
        elif SKIP_LAST_ACTIVITY:
            _online_tab_ids   = ['t1',        't4',       't5',                              't7',              't8']
            _online_tab_names = ['Overview', 'Colleges', 'Counsellor_TVA',                'Supervisor_MoM', 'University_MoM']
        else:
            _online_tab_ids   = ['t1',        't4',       't5',                't6',           't7',              't8']
            _online_tab_names = ['Overview', 'Colleges', 'Counsellor_TVA', 'Last_Activity', 'Supervisor_MoM', 'University_MoM']
        online_pngs = [
            os.path.join(OUTPUT_DIR, f'Online_LMS_{name}_{RUN_STAMP}.png')
            for name in _online_tab_names
        ]
        online_png_ok = await screenshot_html_tabs(online_html, _online_tab_ids, online_pngs, sidebar_nav=SIDEBAR_NAV)
        online_pngs = [p for p, ok in zip(online_pngs, online_png_ok) if ok]

    # ── STEP 2: Regular LMS ────────────────────────────────────────────────
    if LAST_ACTIVITY_ONLY:
        print("─── STEP 2/3: Regular LMS skipped (--last-activity-only) ───────────")
        regular_html = None
        regular_pngs = []
        regular_summary = None
    else:
        print("─── STEP 2/3: Regular LMS Report ───────────────────────────────────")
        print("  [2a-pre] Syncing Regular_Targets period dates...")
        try:
            _month_start = report_date.replace(day=1).strftime('%Y-%m-%d')
            _month_end   = report_date.replace(day=calendar.monthrange(report_date.year, report_date.month)[1]).strftime('%Y-%m-%d')
            sync_regular_dates(_month_start, _month_end, REG_WEEK_START, REG_WEEK_END)
        except Exception as e:
            print(f"  Warning: Could not sync Regular_Targets dates (non-fatal): {e}")
        regular_summary = None
        try:
            print("  [2a] Fetching regular LMS data from DB (REGULAR + CGC + AMITY)...")
            reg_adm, reg_forms = await regular_get_data()
            print("  [2b] Preparing regular LMS data...")
            regular_sheets = regular_prepare_data(reg_adm, reg_forms)
            print("  [2b2] Building Amity YoY total-forms table...")
            try:
                amity_ly = amity_get_total_forms_last_year()
                amity_ty = await amity_get_total_forms_this_year()
                amity_yoy_df = amity_build_yoy_table(amity_ly, amity_ty)
                print("  ✅ Amity Total Forms YoY table built")
            except Exception as e_yoy:
                print(f"  ⚠️  Amity Total Forms YoY failed (non-fatal): {e_yoy}")
                amity_yoy_df = None
            try:
                amity_adm_ly = amity_get_admissions_last_year()
                amity_adm_ty = await amity_get_admissions_this_year()
                amity_adm_df = amity_build_admission_yoy_table(amity_adm_ly, amity_adm_ty)
                print("  ✅ Amity Admissions YoY table built")
            except Exception as e_adm:
                print(f"  ⚠️  Amity Admissions YoY failed (non-fatal): {e_adm}")
                amity_adm_df = None
            print("  [2c] Generating regular LMS HTML report...")
            regular_html, regular_summary = regular_generate_html(regular_sheets, amity_yoy_df=amity_yoy_df, amity_adm_df=amity_adm_df)
            print("  ✅ STEP 2 complete — Regular LMS report generated")
            print()
        except Exception as e:
            print(f"  ❌ STEP 2 FAILED: {e}")
            import traceback
            traceback.print_exc(file=sys.stdout)
            regular_html = None

        # ── STEP 2d: Screenshot Regular tabs ──────────────────────────────
        regular_pngs = []
        if regular_html:
            print("  [2d] Taking screenshots of Regular LMS tabs...")
            _reg_tab_ids   = ['tab-admissions', 'tab-forms', 'tab-amity-forms', 'tab-amity-adm', 'tab-college-mom', 'tab-college-12day']
            _reg_tab_names = ['Admissions',     'Forms',     'Amity_Forms',      'Amity_Adm',     'College_MoM',     'College_12Day']
            regular_pngs = [
                os.path.join(OUTPUT_DIR, f'Regular_LMS_{name}_{RUN_STAMP}.png')
                for name in _reg_tab_names
            ]
            reg_png_ok = await screenshot_html_tabs(regular_html, _reg_tab_ids, regular_pngs, sidebar_nav=SIDEBAR_NAV)
            regular_pngs = [p for p, ok in zip(regular_pngs, reg_png_ok) if ok]

    # ── STEP 3: Send Screenshots via WhatsApp ─────────────────────────────
    print("─── STEP 3/3: Sending Screenshots to WhatsApp + Logging ────────────")
    # Caption for each report file — edit CAPTION_GROUP_MAP (top of file) to change routing
    _tab_labels = {
        'Online_LMS_Overview':         'Owner wise Achievement Report - Online Business',
        'Online_LMS_Colleges':         'Online LOB - University wise Forms & Adm',
        'Online_LMS_Counsellor_TVA':   'Counsellor Targets vs Achievements — Fee & Admissions',
        **({} if SKIP_LAST_ACTIVITY else {'Online_LMS_Last_Activity': 'Admission and Application Ageing Report'}),
        'Online_LMS_Supervisor_MoM':   f'Till Date Supervisor Month on Month — {LAST_MONTH_LABEL} vs {MONTH_LABEL}',
        'Online_LMS_University_MoM':   f'Till Date University Month on Month — {LAST_MONTH_LABEL} vs {MONTH_LABEL}',
        'Regular_LMS_Admissions':   'Admission Target vs Achieved',
        'Regular_LMS_Forms':        'Form Target vs Achieved',
        'Regular_LMS_Amity_Forms':  'Amity Total Forms - Campus YoY',
        'Regular_LMS_Amity_Adm':    'Amity Admissions - Campus YoY',
        'Regular_LMS_College_MoM':   f'College Wise Monthly Comparison — {LAST_MONTH_LABEL} vs {MONTH_LABEL}',
        'Regular_LMS_College_12Day': f'College {_n_days}-Day Comparison — {PREV_MONTH_LABEL} vs {MONTH_LABEL}',
    }

    def _resolve_groups(caption):
        """Return deduped list of group IDs: specific group(s) + All Reports (if configured).
        CAPTION_GROUP_MAP value can be a single group name (str) or a list of group names.
        """
        for prefix, group_val in CAPTION_GROUP_MAP.items():
            if caption.startswith(prefix):
                names = group_val if isinstance(group_val, list) else [group_val]
                specific = []
                for name in names:
                    specific += GROUPS.get(name, [])
                break
        else:
            specific = GROUPS['Online Admission Team']
        all_reports = GROUPS.get('All Reports', [])
        return list(dict.fromkeys(specific + all_reports))  # dedup, preserve order

    all_pngs = online_pngs + regular_pngs
    whapi_results = {}
    if LOCAL_MODE:
        print(f"  [3a] LOCAL MODE — skipping WhatsApp, files saved in: {OUTPUT_DIR}")
        for png_path in all_pngs:
            print(f"       📁 {os.path.basename(png_path)}")
    else:
        print(f"  [3a] Sending {len(all_pngs)} tab screenshots via WHAPI...")
        for png_path in all_pngs:
            base = os.path.basename(png_path)
            key = '_'.join(base.replace('.png', '').split('_')[:-2])
            cap = _tab_labels.get(key, base)
            group_ids = _resolve_groups(cap)
            sent = all(send_via_whapi(png_path, f"{cap} — {FTD_DATE}", group_id=gid) for gid in group_ids)
            whapi_results[key] = sent

    # ── STEP 3b: Log to Google Sheets Report_Logs ─────────────────────────
    print("  [3b] Logging to Google Sheets Report_Logs...")
    log_report(FTD_DATE, "Online LMS",  online_summary  or "", any(whapi_results.get(k) for k in ('Online_LMS_Overview',)))
    log_report(FTD_DATE, "Regular LMS", regular_summary or "", any(whapi_results.get(k) for k in ('Regular_LMS_Admissions',)))

    # ── STEP 4: Write delivery manifest (for cron agent) ───────────────────
    manifest = {
        "date": FTD_DATE,
        "generated_at": datetime.now(UTC).isoformat() + "Z",
        "files": []
    }
    for png_path in all_pngs:
        if os.path.exists(png_path):
            manifest["files"].append({
                "path": os.path.abspath(png_path),
                "filename": os.path.basename(png_path),
                "size_bytes": os.path.getsize(png_path)
            })

    manifest_path = os.path.join(OUTPUT_DIR, f"delivery_manifest_{RUN_STAMP}.json")
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)
    print(f"\n📋 Delivery manifest: {manifest_path}")

    # ── STEP 5: Cleanup output folder ─────────────────────────────────────
    if NO_CLEANUP:
        print("\n─── Cleanup skipped (--nocleanup) ───────────────────────────────")
    else:
        print("\n─── Cleaning up output folder ───────────────────────────────────")
        removed = 0
        for item in os.listdir(OUTPUT_DIR):
            item_path = os.path.join(OUTPUT_DIR, item)
            try:
                if os.path.isfile(item_path):
                    os.remove(item_path)
                    removed += 1
                # Never delete subdirectories — other concurrent scripts (e.g. bhugoal) use them
            except Exception as e:
                print(f"   ⚠️  Could not remove: {item} — {e}")
        print(f"   Cleaned: {removed} items removed from {OUTPUT_DIR}")

    print()
    print("=" * 60)
    print("📊 ALL REPORTS GENERATED SUCCESSFULLY")
    print("=" * 60)
    print(f"   FTD: {FTD_DATE}")
    print(f"   Online screenshots:  {'✅' if online_pngs else '❌'} ({len(online_pngs)}/7)")
    print(f"   Regular screenshots: {'✅' if regular_pngs else '❌'} ({len(regular_pngs)}/6)")
    print(f"   WHAPI sends: {'skipped (--local mode)' if LOCAL_MODE else ('attempted' if WHAPI_TOKEN else 'skipped (no token)')}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
