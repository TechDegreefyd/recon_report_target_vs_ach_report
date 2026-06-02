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
# --local  →  skip WhatsApp, keep files in OUTPUT_DIR instead of sending
LOCAL_MODE    = '--local'     in sys.argv
NO_CLEANUP    = '--nocleanup' in sys.argv

import pandas as pd
import asyncpg
import requests
from datetime import datetime, timedelta, UTC
from dotenv import load_dotenv

from sheets_config import load_online_config, load_regular_config, log_report, get_service

# ─── PATHS ──────────────────────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.environ.get('WORKSPACE_DIR', _SCRIPT_DIR)
load_dotenv(os.path.join(BASE_DIR, '.env'))

OUTPUT_DIR = os.path.join(BASE_DIR, 'Automation Cron Job', 'Target Report')
LOCAL_FALLBACK_DIR = os.path.join(BASE_DIR, 'Automation Cron Job', 'Target Report', 'local_fallback')
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(LOCAL_FALLBACK_DIR, exist_ok=True)

# ─── WHAPI ──────────────────────────────────────────────────────────────────────
WHAPI_TOKEN = os.getenv('WHAPI_TOKEN')
WHATSAPP_GROUP = os.getenv('WHATSAPP_GROUP', '120363426619711887@g.us')

# ─── DATE LOGIC (IST, before 6AM = previous day) ────────────────────────────────
# Override with REPORT_DATE=YYYY-MM-DD env var to run for a specific date
if os.getenv('REPORT_DATE'):
    report_date = datetime.strptime(os.getenv('REPORT_DATE'), '%Y-%m-%d')
else:
    now_utc = datetime.now(UTC)
    now_ist = now_utc + timedelta(hours=5, minutes=30)
    report_date = now_ist - timedelta(days=1) if now_ist.hour < 6 else now_ist

FTD_DATE = report_date.strftime('%Y-%m-%d')
MTD_START = report_date.replace(day=1).strftime('%Y-%m-%d')
MTD_END = FTD_DATE
MONTH_LABEL = report_date.strftime('%B %Y')
MONTH_SHORT = report_date.strftime('%b')

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
ONLINE_SUPERVISOR_TARGETS = online_config.get("supervisor_targets", {})
ONLINE_REVENUE_TARGETS = online_config.get("counsellor_targets", {})

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

    # Counsellors whose supervisor assignment is wrong in the Google Sheet.
    # Correct here until the sheet is updated.
    _SUPERVISOR_OVERRIDES = {
        'Vishwajeet': 'Vishal Gaur',
        'Amit Kumar':  'Vishal Gaur',
    }

    couns_data = []
    for sup, couns_dict in ONLINE_REVENUE_TARGETS.items():
        for couns_name in couns_dict.keys():
            effective_sup = _SUPERVISOR_OVERRIDES.get(couns_name, sup)
            couns_data.append({'supervisor_name': effective_sup, 'counsellor_name': couns_name})
    df_couns = pd.DataFrame(couns_data)

    YTD_START = f'{report_date.year}-01-01'

    adm_query = f"""
    SELECT DISTINCT ON (s.student_id, uc.course_id)
        s.student_id,
        s.student_name,
        s.student_email,
        csj.created_at AT TIME ZONE 'Asia/Kolkata' AS created_at,
        uc.course_id,
        uc.university_name AS college_name,
        uc.course_name,
        INITCAP(TRIM(csj.fee_type)) AS fee_type,
        uc.total_fees AS total_fee,
        uc.semester_fees AS sem_fee,
        uc.annual_fees AS annual_fee,
        csj.deposit_amount AS fee_deposit,
        uc.duration || ' ' || uc.duration_type AS course_duration,
        c.counsellor_name,
        c.counsellor_id
    FROM students s
    JOIN course_status_journeys csj ON s.student_id = csj.student_id
    JOIN university_courses uc ON csj.course_id = uc.course_id
    LEFT JOIN counsellors c ON s.assigned_counsellor_id = c.counsellor_id
    WHERE csj.course_status = 'Admission'
      AND COALESCE(csj.fee_type, '') NOT ILIKE '%partial%'
      AND csj.created_at >= '{YTD_START}'::date
    ORDER BY s.student_id, uc.course_id, csj.created_at ASC;
    """

    form_query = f"""
    SELECT DISTINCT ON (s.student_id, uc.course_id)
        s.student_id,
        uc.university_name AS college_name,
        csj.created_at AT TIME ZONE 'Asia/Kolkata' AS created_at
    FROM students s
    JOIN course_status_journeys csj ON s.student_id = csj.student_id
    JOIN university_courses uc ON csj.course_id = uc.course_id
    WHERE csj.course_status = 'Application'
      AND csj.created_at >= '{YTD_START}'::date
    ORDER BY s.student_id, uc.course_id, csj.created_at ASC;
    """

    adm_rows = await conn.fetch(adm_query)
    form_rows = await conn.fetch(form_query)
    await conn.close()

    df_adm = pd.DataFrame([dict(r) for r in adm_rows])
    df_form = pd.DataFrame([dict(r) for r in form_rows])

    for df in (df_couns, df_adm):
        if not df.empty:
            for col in ('supervisor_name', 'counsellor_name'):
                if col in df.columns:
                    # Normalize whitespace AND case so sheet names match DB names exactly
                    df[col] = df[col].astype(str).str.replace(r'\s+', ' ', regex=True).str.strip().str.title()

    # After title-casing, deduplicate the roster — the sheet may have the same counsellor
    # listed twice with different capitalisation (e.g. 'Virat Kumar Singh' / 'Virat kumar singh').
    if not df_couns.empty:
        df_couns = df_couns.drop_duplicates(subset=['supervisor_name', 'counsellor_name'])

    # SQL DISTINCT ON (student_id, course_id) guarantees one row per student+course.
    # No further Python dedup needed for either admissions or forms.

    return df_couns, df_adm, df_form


def online_pct(achieved, target):
    achieved = 0 if pd.isna(achieved) else achieved
    target = 0 if pd.isna(target) else target
    if isinstance(achieved, float) and achieved.is_integer():
        achieved = int(achieved)
    if isinstance(target, float) and target.is_integer():
        target = int(target)
    if target == 0 or pd.isna(target):
        return "0.0%"
    return f"{(achieved / target * 100):.1f}%"


def online_prepare_data(df_couns, df_adm, df_form):
    """Build all Online report DataFrames in memory — no Excel involved."""
    # Force IST date strings regardless of whether asyncpg returns tz-naive or tz-aware timestamps
    def _to_ist_date(series):
        if series.empty:
            return pd.Series(dtype='object')
        ts = pd.to_datetime(series)
        if ts.dt.tz is None:
            ts = ts.dt.tz_localize('Asia/Kolkata', ambiguous='infer')
        else:
            ts = ts.dt.tz_convert('Asia/Kolkata')
        return ts.dt.strftime('%Y-%m-%d')

    df_adm['date_str']  = _to_ist_date(df_adm['created_at'])  if not df_adm.empty  else pd.Series(dtype='object')
    df_form['date_str'] = _to_ist_date(df_form['created_at']) if not df_form.empty else pd.Series(dtype='object')

    adm_ftd  = df_adm[df_adm['date_str'] == FTD_DATE]
    form_ftd = df_form[df_form['date_str'] == FTD_DATE]
    adm_mtd  = df_adm[df_adm['date_str'].between(MTD_START, MTD_END)]
    form_mtd = df_form[df_form['date_str'].between(MTD_START, MTD_END)]

    # Online targets are monthly — all achievement uses MTD data, not a week window
    known_counsellors = set(df_couns['counsellor_name'].tolist()) if not df_couns.empty else set()
    if not adm_mtd.empty:
        unmatched = adm_mtd[~adm_mtd['counsellor_name'].isin(known_counsellors)]
        if not unmatched.empty:
            print("WARNING: Admissions found for counsellors missing from sheet")
            print(unmatched[['student_id', 'college_name', 'counsellor_name', 'fee_deposit', 'created_at']].to_string(index=False))

    # Dynamic — order driven by Google Sheets Online_Targets row order
    sup_order     = list(ONLINE_SUPERVISOR_TARGETS.keys())
    display_names = {s: s for s in sup_order}   # use exact sheet name as display name

    # ── Counsellor fee data (MTD) ────────────────────────────────────────────
    # D1 rule: deduplicate by student before summing fees — a student with multiple
    # college rows would otherwise have their total fee counted once per college.
    # Keep the row with the highest deposit_amount for each student (or earliest if tied).
    def _dedup_fees(df):
        if df.empty:
            return df
        return (df.sort_values('fee_deposit', ascending=False)
                  .drop_duplicates(subset=['student_id'], keep='first'))

    adm_mtd_fees = _dedup_fees(adm_mtd)
    adm_ftd_fees = _dedup_fees(adm_ftd)

    couns_rev_week = adm_mtd_fees.groupby('counsellor_name')['fee_deposit'].sum().reset_index(name='Achieved')
    couns_rev_ftd  = adm_ftd_fees.groupby('counsellor_name')['fee_deposit'].sum().reset_index(name='FTD')
    df_c_rev = df_couns.merge(couns_rev_week, on='counsellor_name', how='left') \
                       .merge(couns_rev_ftd,  on='counsellor_name', how='left').fillna(0)
    df_c_rev['Target'] = df_c_rev.apply(lambda r: ONLINE_REVENUE_TARGETS.get(r['supervisor_name'], {}).get(r['counsellor_name'], 0), axis=1)
    df_c_rev['Ach %']  = df_c_rev.apply(lambda r: online_pct(r['Achieved'], r['Target']), axis=1)

    # ── Counsellor admission data (MTD) — count distinct students, not rows ──
    couns_adm_week = adm_mtd.groupby('counsellor_name')['student_id'].nunique().reset_index(name='Achieve')
    couns_adm_ftd  = adm_ftd.groupby('counsellor_name')['student_id'].nunique().reset_index(name='FTD')
    df_c_adm = df_couns.merge(couns_adm_week, on='counsellor_name', how='left') \
                       .merge(couns_adm_ftd,  on='counsellor_name', how='left').fillna(0)

    # ── Counsellor_Fee_Collected ─────────────────────────────────────────────
    rows = []
    for sup in sup_order:
        s = df_c_rev[df_c_rev['supervisor_name'] == sup]
        if s.empty:
            continue
        for _, r in s.iterrows():
            rows.append([display_names.get(sup, sup), r['counsellor_name'], r['Target'], r['Achieved'], r['Ach %'], r['FTD']])
        rows.append([f'Total ({display_names.get(sup, sup)})', '', s['Target'].sum(), s['Achieved'].sum(),
                     online_pct(s['Achieved'].sum(), s['Target'].sum()), s['FTD'].sum()])
    rows.append(['Grand Total', '', df_c_rev['Target'].sum(), df_c_rev['Achieved'].sum(),
                 online_pct(df_c_rev['Achieved'].sum(), df_c_rev['Target'].sum()), df_c_rev['FTD'].sum()])
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
    uni_ytd_f = df_form.groupby('college_name').size().reset_index(name='YTD Forms')
    uni_ytd_a = df_adm.groupby('college_name')['student_id'].nunique().reset_index(name='YTD Admissions')
    uni_mtd_f = form_mtd.groupby('college_name').size().reset_index(name='MTD Forms')
    uni_mtd_a = adm_mtd.groupby('college_name')['student_id'].nunique().reset_index(name='MTD Admissions')
    uni_ftd_f = form_ftd.groupby('college_name').size().reset_index(name='FTD Forms')
    uni_ftd_a = adm_ftd.groupby('college_name')['student_id'].nunique().reset_index(name='FTD Admissions')
    df_uni = uni_ytd_f.merge(uni_ytd_a, on='college_name', how='outer') \
                      .merge(uni_mtd_f,  on='college_name', how='outer') \
                      .merge(uni_mtd_a,  on='college_name', how='outer') \
                      .merge(uni_ftd_f,  on='college_name', how='outer') \
                      .merge(uni_ftd_a,  on='college_name', how='outer').fillna(0)
    df_uni = df_uni.sort_values('YTD Forms', ascending=False)
    rows = []
    for _, r in df_uni.iterrows():
        c_name = r['college_name'].title()
        for wrong, right in [('Gla', 'GLA'), ('Lpu', 'LPU'), ('Iit', 'IIT'), ('Nit', 'NIT'), ('Smu', 'SMU'), ('Nmims', 'NMIMS')]:
            c_name = c_name.replace(wrong, right)
        rows.append([c_name, int(r['YTD Forms']), int(r['YTD Admissions']), online_pct(r['YTD Admissions'], r['YTD Forms']),
                     int(r['MTD Forms']), int(r['MTD Admissions']), online_pct(r['MTD Admissions'], r['MTD Forms']),
                     int(r['FTD Forms']), int(r['FTD Admissions']), online_pct(r['FTD Admissions'], r['FTD Forms'])])
    rows.append(['Total', int(df_uni['YTD Forms'].sum()), int(df_uni['YTD Admissions'].sum()),
                 online_pct(df_uni['YTD Admissions'].sum(), df_uni['YTD Forms'].sum()),
                 int(df_uni['MTD Forms'].sum()), int(df_uni['MTD Admissions'].sum()),
                 online_pct(df_uni['MTD Admissions'].sum(), df_uni['MTD Forms'].sum()),
                 int(df_uni['FTD Forms'].sum()), int(df_uni['FTD Admissions'].sum()),
                 online_pct(df_uni['FTD Admissions'].sum(), df_uni['FTD Forms'].sum())])
    df_college_sheet = pd.DataFrame(rows, columns=['Colleges', 'YTD Forms', 'YTD Admissions', 'YTD F2A %',
                                                    'MTD Forms', 'MTD Admissions', 'MTD F2A %',
                                                    'FTD Forms', 'FTD Admissions', 'FTD F2A %'])

    print("✅ Online LMS data prepared")
    return {
        'Counsellor_Fee_Collected': df_c_rev_sheet,
        'Supervisor_Fee_Collected': df_sup_rev_sheet,
        'Counsellor_Admission':     df_c_adm_sheet,
        'Supervisor_Admission':     df_sup_adm_sheet,
        'College_Performance':      df_college_sheet,
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
            return f'\u20b9{v / 100000:.1f}L'.replace('.0L', 'L')
        return f'\u20b9{v:,.0f}'

    def money_full(v):
        try:
            return f'\u20b9{float(v):,.0f}' if float(v) else '\u20b90'
        except:
            return '\u2014'

    def num(v):
        try:
            if pd.isna(v) or float(v) == 0:
                return '\u2014'
            return f'{int(float(v)):,}'
        except:
            return esc(v)

    def pct_value(s):
        text = str(s)
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
        if zero:
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
        return f'<span class="pct {pct_class(p, zero)}">{esc(pct_val)}</span>'

    def online_pct(a, t):
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
<div class="sup-row"><span class="sup-metric">Fee Ach %</span><span class="sup-val {bar_color}">{fee_pct_str}</span><div class="sup-ratio">{money(fee_ach).replace('\u20b9','')}/{money(fee_tgt).replace('\u20b9','')}</div></div>
<div class="sup-row"><span class="sup-metric">Admissions</span><span class="sup-val">{adm_ach_val}/{adm_tgt}</span></div>
<div class="sup-row"><span class="sup-metric">FTD</span><span class="sup-val">{num(ftd_adm) if ftd_adm else 0} adm &middot; {money(ftd_fee)}</span></div>
<div class="sup-bar-bg"><div class="sup-bar" style="width:{bar_width}%"></div></div></div>\n'''

        # Summary table row
        adm_pct_str = online_pct(adm_ach_val, adm_tgt)
        sup_summary_rows += f'''<tr><td class="left bold">{disp_name}</td><td>{adm_tgt}</td><td>{adm_ach_val}</td><td>{pill(adm_pct_str)}</td><td class="rev">{money_full(fee_tgt)}</td><td class="rev">{money_full(fee_ach)}</td><td>{pill(fee_pct_str)}</td><td class="ftd">{money_full(ftd_fee)}</td><td>{num(ftd_adm) if ftd_adm else 0}</td></tr>\n'''

    # Grand totals
    gt_fee_ach = float(gt['Fee Collected'])
    gt_fee_tgt = float(gt['Target'])
    gt_fee_ftd = float(gt['FTD'])
    gt_adm_ach = int(gt_adm['Achieve'])
    gt_adm_ftd = int(float(gt_adm['FTD'])) if float(gt_adm['FTD']) else 0
    gt_fee_pct_str = online_pct(gt_fee_ach, gt_fee_tgt)

    sup_summary_rows += f'''
<tr class="grand-total"><td class="left bold">&#9679; Grand Total</td><td>{total_adm_target}</td><td>{gt_adm_ach}</td><td>{pill(online_pct(gt_adm_ach, total_adm_target))}</td><td class="rev">{money_full(gt_fee_tgt)}</td><td class="rev">{money_full(gt_fee_ach)}</td><td>{pill(gt_fee_pct_str)}</td><td class="ftd">{money_full(gt_fee_ftd)}</td><td>{num(gt_adm_ftd)}</td></tr>'''

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
    CSS = r'''
.sup-ratio, .kpi-ratio { font-size: 11px; opacity: 0.8; margin-top: 2px; font-weight: 400; }
.sup-val-ratio { font-size: 10px; display: block; opacity: 0.7; }
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}:root{--white:#fff;--off:#f8f8f6;--border:#e8e4dc;--border-dark:#c8c0b4;--ink:#1a1a18;--ink-mid:#555550;--ink-light:#9a9590;--gold:#c8a84b;--gold-light:#f5ecd4;--green:#2d7a4f;--green-bg:#e8f5ee;--amber:#b86e1c;--amber-bg:#fdf3e4;--orange:#c05e0a;--orange-bg:#fde8d4;--red:#c0392b;--red-bg:#fdecea;--gray:#888880;--gray-bg:#f0efec;--blue:#1a4a8a;--blue-bg:#e8eef7;--radius:3px}body{background:var(--white);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:var(--ink);min-height:100vh;overflow-x:hidden}#t1,#t2,#t3,#t4{display:none}.shell{max-width:960px;margin:0 auto;padding:0 14px 48px}.header{padding:26px 0 18px;border-bottom:2px solid var(--ink);margin-bottom:22px}.header-top{display:flex;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;gap:8px}.brand{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--ink-light);margin-bottom:5px}.title{font-size:clamp(20px,4.5vw,30px);font-weight:400;letter-spacing:-.01em;line-height:1.15}.header-meta{text-align:right}.badge{display:inline-block;background:var(--ink);color:var(--white);font-size:9px;letter-spacing:.14em;text-transform:uppercase;padding:3px 8px;border-radius:var(--radius);margin-bottom:4px}.date{font-size:11px;color:var(--ink-light);letter-spacing:.04em}.tabs{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:6px;margin-bottom:22px;position:sticky;top:0;z-index:100;background:var(--white);padding:8px 0}.tab-label{display:flex;align-items:center;justify-content:center;gap:4px;padding:9px 6px;cursor:pointer;font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--ink-mid);background:var(--off);border:1.5px solid var(--border-dark);border-radius:var(--radius);user-select:none;-webkit-tap-highlight-color:transparent;white-space:nowrap}#t1:checked~.shell label[for=t1],#t2:checked~.shell label[for=t2],#t3:checked~.shell label[for=t3],#t4:checked~.shell label[for=t4]{background:var(--ink);color:var(--white);border-color:var(--ink)}.panel{display:none}#t1:checked~.shell #p1,#t2:checked~.shell #p2,#t3:checked~.shell #p3,#t4:checked~.shell #p4{display:block}.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin-bottom:24px}.kpi{background:var(--off);border:1px solid var(--border);border-radius:var(--radius);padding:16px 18px}.kpi-label{font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--ink-light);margin-bottom:3px}.kpi-value{font-size:26px;font-weight:700;line-height:1.1;letter-spacing:-.02em}.kpi-value.orange{color:var(--orange)}.kpi-value.green{color:var(--green)}.kpi-value.amber{color:var(--amber)}.kpi-value.red{color:var(--red)}.kpi-sub{font-size:11px;color:var(--ink-mid);margin-top:2px}.slabel{font-size:12px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-light);margin-bottom:12px;padding-bottom:6px;border-bottom:1px solid var(--border)}.sup-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:24px}.sup-card{background:var(--white);border:1px solid var(--border);border-radius:5px;padding:11px 12px 10px}.sup-name{font-size:14px;font-weight:600;margin-bottom:6px;letter-spacing:-.01em}.sup-name small{display:block;font-size:9px;font-weight:500;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-light);margin-bottom:1px}.sup-row{display:flex;justify-content:space-between;align-items:baseline;padding:2px 0;font-size:12px}.sup-metric{color:var(--ink-mid);font-size:10px}.sup-val{font-weight:600;white-space:nowrap;font-size:12px}.sup-val.orange{color:var(--orange)}.sup-val.red{color:var(--red)}.sup-val.green{color:var(--green)}.sup-val.amber{color:var(--amber)}.sup-bar-bg{background:var(--border);height:4px;border-radius:2px;margin-top:8px;overflow:hidden}.sup-bar{height:100%;border-radius:2px;background:var(--green);transition:width .4s}.table-wrap{overflow-x:auto;border-radius:4px;border:1px solid var(--border);background:var(--white)}table{width:100%;border-collapse:collapse;font-size:13px}th{background:var(--ink);color:var(--white);padding:9px 8px;text-align:center;font-weight:600;font-size:10.5px;letter-spacing:.04em;border-right:1px solid rgba(255,255,255,.1)}th:last-child{border-right:none}th.left,td.left{text-align:left;padding-left:12px}td{text-align:center;padding:7px 8px;border-bottom:1px solid var(--border);white-space:nowrap;font-variant-numeric:tabular-nums}tr:last-child td{border-bottom:none}.bold{font-weight:600}.rev{font-variant-numeric:tabular-nums;font-weight:500}.ftd{color:var(--ink-mid)}.ytd{font-weight:500}.pct{font-weight:600;font-size:11px;padding:2px 7px;border-radius:2px;display:inline-block}.pct.green{background:var(--green-bg);color:var(--green)}.pct.amber{background:var(--amber-bg);color:var(--amber)}.pct.orange{background:var(--orange-bg);color:var(--orange)}.pct.red{background:var(--red-bg);color:var(--red)}.pct.zero{color:var(--gray);background:var(--gray-bg)}.sup-header td{background:var(--gold-light)!important;font-weight:700;border-top:2px solid var(--gold)!important;padding:5px 12px!important;font-size:11px;letter-spacing:.06em;text-transform:uppercase}.sub-total td{background:var(--off)!important;border-top:1px solid var(--border-dark)!important;font-weight:600!important}.grand-total td{font-weight:700!important;border-top:2px solid var(--ink)!important;background:var(--off)!important}.varun .sup-bar{background:var(--blue)}.sunil .sup-bar{background:var(--green)}.vishal .sup-bar{background:var(--orange)}.siddhartha .sup-bar{background:var(--amber)}.legend{display:flex;gap:16px;margin-top:14px;font-size:11px;color:var(--ink-mid)}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}.dot.green{background:var(--green)}.dot.amber{background:var(--amber)}.dot.orange{background:var(--orange)}.dot.red{background:var(--red)}.footer{margin-top:28px;padding:16px 0 8px;border-top:1px solid var(--border);font-size:11px;color:var(--ink-light);text-align:center}.footer-brand{font-weight:600;color:var(--ink-mid);margin-top:4px}@media(max-width:600px){.kpis{grid-template-columns:1fr 1fr}.kpi-value{font-size:22px}table{font-size:12px}.sup-cards{grid-template-columns:1fr}.header-top{flex-wrap:nowrap;align-items:flex-start}.header-meta{flex-shrink:0;min-width:fit-content}}
'''

    p1 = '<input type="radio" name="dash" id="t1" checked><input type="radio" name="dash" id="t2"><input type="radio" name="dash" id="t3"><input type="radio" name="dash" id="t4">'
    p2 = f'<div class="shell"><header class="header"><div class="header-top"><div><div class="brand">Performance Intelligence &middot; Online</div><h1 class="title">Online Admissions &amp; Fee Collected Tracker</h1></div><div class="header-meta"><div class="badge">Live Report</div><div class="date">{MONTH_LABEL} &middot; FTD {report_date.strftime("%d %b")}</div></div></div></header>'
    p3 = '<div class="tabs"><label class="tab-label" for="t1">&#x1f4ca; Overview</label><label class="tab-label" for="t2">&#x1f4b0; Fee Collected</label><label class="tab-label" for="t3">&#x1f393; Admissions</label><label class="tab-label" for="t4">&#x1f3eb; Colleges</label></div>'
    p4_overview_kpis = f'<div class="kpis"><div class="kpi"><div class="kpi-label">Total Fee Collected</div><div class="kpi-value {pct_class((gt_fee_ach / gt_fee_tgt * 100) if gt_fee_tgt else 0)}">{money(gt_fee_ach)}</div><div class="kpi-sub">of {money(gt_fee_tgt)} target</div></div><div class="kpi"><div class="kpi-label">Fee Ach %</div><div class="kpi-value {pct_class((gt_fee_ach / gt_fee_tgt * 100) if gt_fee_tgt else 0)}">{gt_fee_pct_str}</div><div class="kpi-ratio">{money(gt_fee_ach).replace(chr(0x20b9), "")}/{money(gt_fee_tgt).replace(chr(0x20b9), "")}</div><div class="kpi-sub">Grand Total</div></div><div class="kpi"><div class="kpi-label">Admissions</div><div class="kpi-value {pct_class(adm_ach_pct)}">{adm_ach_pct:.1f}%</div><div class="kpi-ratio">{gt_adm_ach}/{total_adm_target}</div><div class="kpi-sub">Grand Total</div></div></div>'
    p4 = f'<section class="panel" id="p1">{p4_overview_kpis}<div class="slabel"><span>Team Owner Snapshot</span></div><div class="sup-cards">{sup_card_html}</div><div class="slabel"><span>Team Owner Summary Table</span></div><div class="table-wrap"><table><thead><tr><th class="left">Team Owner</th><th>Adm TG</th><th>Adm Ach</th><th>Adm %</th><th>Fee TG</th><th>Fee Ach</th><th>Fee Ach %</th><th>FTD Fee</th><th>FTD Adm</th></tr></thead><tbody>{sup_summary_rows}</tbody></table></div></section>'
    p5_fee_kpis = f'<div class="kpis"><div class="kpi"><div class="kpi-label">Fee Target</div><div class="kpi-value">{money(gt_fee_tgt)}</div><div class="kpi-sub">{MONTH_LABEL}</div></div><div class="kpi"><div class="kpi-label">Achieved</div><div class="kpi-value {pct_class((gt_fee_ach / gt_fee_tgt * 100) if gt_fee_tgt else 0)}">{money(gt_fee_ach)}</div><div class="kpi-sub">{gt_fee_pct_str} overall</div></div><div class="kpi"><div class="kpi-label">FTD</div><div class="kpi-value green">{money(gt_fee_ftd)}</div><div class="kpi-sub">Today\'s fee collected</div></div></div>'
    p5 = f'<section class="panel" id="p2">{p5_fee_kpis}<div class="slabel"><span>Counsellor-wise Fee Collected Breakdown &middot; Counsellor targets are intentionally zero</span></div><div class="table-wrap"><table><thead><tr><th class="left">Counsellor</th><th>Target</th><th>MTD Achieved</th><th>Ach %</th><th>FTD</th></tr></thead><tbody>{c_rev_rows}</tbody></table></div></section>'
    p6_adm_kpis = f'<div class="kpis"><div class="kpi"><div class="kpi-label">Adm Target</div><div class="kpi-value">{total_adm_target}</div><div class="kpi-sub">{MONTH_LABEL}</div></div><div class="kpi"><div class="kpi-label">Achieved</div><div class="kpi-value">{gt_adm_ach}</div><div class="kpi-sub">{adm_ach_pct:.1f}%</div></div><div class="kpi"><div class="kpi-label">FTD</div><div class="kpi-value green">{gt_adm_ftd}</div><div class="kpi-sub">Today\'s closes</div></div></div>'
    p6 = f'<section class="panel" id="p3">{p6_adm_kpis}<div class="slabel"><span>Counsellor-wise Admissions</span></div><div class="table-wrap"><table><thead><tr><th class="left">Counsellor</th><th>Achieved</th><th>FTD</th></tr></thead><tbody>{c_adm_rows}</tbody></table></div></section>'
    ytd_from_label = report_date.strftime('1 Jan %Y')
    p7 = f'<section class="panel" id="p4"><div class="slabel"><span>College-wise Performance &middot; Forms to Admissions &middot; YTD from {ytd_from_label}</span></div><div class="table-wrap"><table><thead><tr><th class="left" rowspan="2">College</th><th colspan="3">Year to Date</th><th colspan="3">Month to Date</th><th colspan="3">FTD</th></tr><tr><th>Forms</th><th>Adm</th><th>F2A %</th><th>Forms</th><th>Adm</th><th>F2A %</th><th>Forms</th><th>Adm</th><th>F2A %</th></tr></thead><tbody>{college_rows}</tbody></table></div><div class="legend"><div class="legend-item"><span class="dot green"></span> &ge; 100% &mdash; Exceeding</div><div class="legend-item"><span class="dot amber"></span> 70&ndash;99% &mdash; On Track</div><div class="legend-item"><span class="dot orange"></span> 40&ndash;69% &mdash; Needs Work</div><div class="legend-item"><span class="dot red"></span> &lt; 40% &mdash; Critical</div></div></section>'
    p8 = f'<footer class="footer"><div class="footer-note">F2A = Forms to Admissions conversion. FTD = For The Day. Fee Collected in Indian &#x20b9;.</div><div class="footer-brand">Online &middot; Performance &middot; {MONTH_LABEL}</div></footer></div></body></html>'

    html_doc = f'''<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Online Admissions Dashboard</title><style>{CSS}</style></head><body>
{p1}{p2}{p3}{p4}{p5}{p6}{p7}{p8}'''

    with open(out_path, 'w', encoding='utf-8-sig') as f:
        f.write(html_doc)
    print(f"✅ Online LMS HTML: {out_path}")
    summary = f"Adm: {gt_adm_ach} ({adm_ach_pct:.1f}%), Fee: {money(gt_fee_ach)}, FTD Adm: {gt_adm_ftd}"
    return out_path, summary


# ═══════════════════════════════════════════════════════════════════════════════
# PART 2 — REGULAR LMS REPORT
# ═══════════════════════════════════════════════════════════════════════════════

regular_config = load_regular_config()

REG_WEEK_START = regular_config.get("target_period", {}).get("start_date")
REG_WEEK_END   = regular_config.get("target_period", {}).get("end_date")
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
    csj.created_at AT TIME ZONE 'Asia/Kolkata' AS created_at
FROM students s
JOIN course_status_journeys csj ON s.student_id = csj.student_id
JOIN university_courses uc ON csj.course_id = uc.course_id
WHERE csj.course_status = 'Admission'
  AND COALESCE(csj.fee_type, '') NOT ILIKE '%partial%'
  AND csj.created_at >= '{ytd_start}'::date
  {exclude_clause}
ORDER BY s.student_id, uc.course_id, csj.created_at ASC;"""

# Base forms SQL — 'Walkin Marked' added, optional college exclusion and YTD start injected per DB
_REG_FORM_SQL = """SELECT DISTINCT ON (s.student_id, csj.course_id)
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
  AND csj.created_at >= '{ytd_start}'::date
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


async def regular_get_data():
    ytd_start = f'{report_date.year}-01-01'
    all_adm, all_form = [], []
    db_excludes = {
        "REGULAR": _REGULAR_EXCLUDE,
        "CGC":     _NO_EXCLUDE,
        "AMITY":   _NO_EXCLUDE,
    }
    for db in REGULAR_DB_CONFIGS:
        excl = db_excludes.get(db['name'], _NO_EXCLUDE)
        adm_sql  = _REG_ADM_SQL.format(exclude_clause=excl, ytd_start=ytd_start)
        form_sql = _REG_FORM_SQL.format(exclude_clause=excl, ytd_start=ytd_start)
        conn = await asyncpg.connect(host=db['host'], port=db['port'], database=db['database'],
                                     user=db['user'], password=db['password'])
        all_adm.append(pd.DataFrame([dict(r) for r in await conn.fetch(adm_sql)]))
        all_form.append(pd.DataFrame([dict(r) for r in await conn.fetch(form_sql)]))
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


YTD_START = f'{report_date.year}-01-01'  # Jan 1 of report year


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
SELECT
    campus_location AS campus_name,
    (created_at AT TIME ZONE 'Asia/Kolkata')::date AS form_date
FROM registrations
WHERE college_for_applied ILIKE '%Amity%'
AND created_at >= '{ytd_start}'::date
ORDER BY form_date
"""


async def amity_get_total_forms_this_year():
    """Query REGULAR DB for this year's Amity total forms via registrations table."""
    db = next(d for d in REGULAR_DB_CONFIGS if d['name'] == 'REGULAR')
    ytd_start = f'{report_date.year}-01-01'
    sql = _AMITY_TOTAL_FORMS_SQL.format(ytd_start=ytd_start)
    conn = await asyncpg.connect(host=db['host'], port=db['port'],
                                  database=db['database'], user=db['user'], password=db['password'])
    rows = await conn.fetch(sql)
    await conn.close()
    records = []
    for r in rows:
        campus = _norm_amity_campus_db(r['campus_name'])
        if campus:
            records.append({'campus': campus, 'date': str(r['form_date'])})
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
AND COALESCE(csj.fee_type, '') NOT ILIKE '%partial%'
AND uc.university_name ILIKE '%Amity%'
AND csj.created_at >= '{ytd_start}'::date
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
    col_date    = next((i for i, v in enumerate(h) if v.strip().lower() == 'date'),   3)
    col_campus  = next((i for i, v in enumerate(h) if v.strip().lower() == 'campus'), 6)
    col_refund  = next((i for i, v in enumerate(h) if 'refund' in v.lower()),         1)
    records = []
    for row in rows[1:]:
        if len(row) <= max(col_date, col_campus):
            continue
        refund = row[col_refund].strip() if len(row) > col_refund else ''
        if refund in ('Drop', 'Lost'):
            continue
        campus = _norm_amity_campus_sheet(row[col_campus])
        if not campus:
            continue
        try:
            dt = datetime.strptime(row[col_date].strip(), '%d-%m-%Y').strftime('%Y-%m-%d')
        except ValueError:
            continue
        records.append({'campus': campus, 'date': dt})
    print(f"  [Last Year Admission] {len(records)} admissions loaded")
    return pd.DataFrame(records) if records else pd.DataFrame(columns=['campus', 'date'])


async def amity_get_admissions_this_year():
    """Query AMITY DB for this year's admissions."""
    db = next(d for d in REGULAR_DB_CONFIGS if d['name'] == 'AMITY')
    ytd_start = f'{report_date.year}-01-01'
    sql = _AMITY_ADM_SQL.format(ytd_start=ytd_start)
    conn = await asyncpg.connect(host=db['host'], port=db['port'],
                                  database=db['database'], user=db['user'], password=db['password'])
    rows = await conn.fetch(sql)
    await conn.close()
    records = []
    for r in rows:
        campus = _norm_amity_campus_db(r['campus_name'])
        if campus:
            records.append({'campus': campus, 'date': str(r['adm_date'])})
    print(f"  [AMITY DB] {len(records)} admissions loaded")
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

    print("✅ Regular LMS data prepared")
    return sheets


def regular_generate_html(sheets, amity_yoy_df=None, amity_adm_df=None):
    """Generate Regular LMS HTML dashboard directly from in-memory DataFrames."""
    out_path = os.path.join(OUTPUT_DIR, f'Degreefyd_Regular_LMS_HTML_Report_{RUN_STAMP}.html')

    adm = sheets['Admissions Data']
    forms = sheets['Forms Data']

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

    CSS = r'''
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--white:#fff;--off:#f8f8f6;--border:#e8e4dc;--border-dark:#c8c0b4;--ink:#1a1a18;--ink-mid:#555550;--ink-light:#9a9590;--gold:#c8a84b;--gold-light:#f5ecd4;--green:#2d7a4f;--green-bg:#e8f5ee;--amber:#b86e1c;--amber-bg:#fdf3e4;--red:#c0392b;--red-bg:#fdecea;--blue:#1a4a8a;--blue-bg:#e8eef7;--radius:3px}
body{background:var(--white);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:var(--ink);min-height:100vh}
#tab-admissions,#tab-forms,#tab-amity-yoy{display:none}
.shell{max-width:900px;margin:0 auto;padding:0 16px 40px}
.header{padding:28px 0 20px;border-bottom:2px solid var(--ink);margin-bottom:24px}
.header-top{display:flex;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;gap:8px}
.brand{font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--ink-light);margin-bottom:6px}
.title{font-size:clamp(22px,5vw,32px);font-weight:400;letter-spacing:-.01em;line-height:1.1;color:var(--ink)}
.header-meta{text-align:right}
.badge{display:inline-block;background:var(--gold);color:var(--white);font-size:9px;letter-spacing:.14em;text-transform:uppercase;padding:3px 8px;border-radius:var(--radius);margin-bottom:4px}
.date{font-size:12px;color:var(--ink-light);letter-spacing:.04em}
.tabs{display:flex;gap:0;margin-bottom:24px;border:1.5px solid var(--border-dark);border-radius:var(--radius);overflow:hidden;position:sticky;top:0;z-index:100;background:var(--white)}
.tab-label{flex:1;display:flex;align-items:center;justify-content:center;gap:8px;padding:12px 16px;cursor:pointer;font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-mid);background:var(--off);user-select:none}
.tab-label:first-of-type{border-right:1.5px solid var(--border-dark)}
#tab-admissions:checked~.shell .tab-label[for=tab-admissions],#tab-forms:checked~.shell .tab-label[for=tab-forms],#tab-amity-forms:checked~.shell .tab-label[for=tab-amity-forms],#tab-amity-adm:checked~.shell .tab-label[for=tab-amity-adm]{background:var(--ink);color:var(--white)}
.panel{display:none}
#tab-admissions:checked~.shell #panel-admissions,#tab-forms:checked~.shell #panel-forms,#tab-amity-forms:checked~.shell #panel-amity-forms,#tab-amity-adm:checked~.shell #panel-amity-adm{display:block}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin-bottom:28px}
.kpi{background:var(--white);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px}
.kpi-label{font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--ink-light);margin-bottom:3px}
.kpi-value{font-size:20px;font-weight:700;line-height:1.1}
.kpi-value.green{color:var(--green)}.kpi-value.amber{color:var(--amber)}.kpi-value.red{color:var(--red)}
.kpi-sub{font-size:11px;color:var(--ink-mid);margin-top:2px}
.section-label{font-size:12px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-light);margin-bottom:12px;padding-bottom:6px;border-bottom:1px solid var(--border)}
.table-wrap{overflow-x:auto;border-radius:6px;border:1px solid var(--border)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{background:var(--ink);color:var(--white);padding:8px 8px;text-align:center;font-weight:600;font-size:10.5px;letter-spacing:.04em;border-right:1px solid rgba(255,255,255,.1)}
th.th-group{background:#2a2a28;color:rgba(255,255,255,.85);font-size:10px}
td{padding:7px 8px;text-align:center;border-bottom:1px solid var(--border)}
tr:last-child td{border-bottom:none}
tr:hover{background:var(--off)}
.college-name{text-align:left;font-weight:500}
.bold{font-weight:700}
.num{font-variant-numeric:tabular-nums}
.pct{font-weight:600;font-size:11px;padding:2px 7px;border-radius:2px;display:inline-block}
.pct.green{background:var(--green-bg);color:var(--green)}
.pct.amber{background:var(--amber-bg);color:var(--amber)}
.pct.red{background:var(--red-bg);color:var(--red)}
.total-row td{background:var(--off);font-weight:600;border-top:2px solid var(--border-dark)}
.legend{display:flex;gap:16px;margin-top:14px;font-size:11px;color:var(--ink-mid)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}
.dot.green{background:var(--green)}.dot.amber{background:var(--amber)}.dot.red{background:var(--red)}
.footer{margin-top:28px;padding:16px 0 8px;border-top:1px solid var(--border);font-size:11px;color:var(--ink-light);text-align:center}
.footer-brand{font-weight:600;color:var(--ink-mid);margin-top:4px}
@media(max-width:600px){.kpis{grid-template-columns:1fr 1fr}.kpi-value{font-size:22px}table{font-size:11.5px}.header-top{flex-wrap:nowrap;align-items:flex-start}.header-meta{flex-shrink:0;min-width:fit-content}}
'''

    html_doc = f'''<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Regular Admissions Dashboard</title><style>{CSS}</style></head><body>
<input type="radio" name="view" id="tab-admissions" checked>
<input type="radio" name="view" id="tab-forms">
<input type="radio" name="view" id="tab-amity-forms">
<input type="radio" name="view" id="tab-amity-adm">
<div class="shell">
<header class="header"><div class="header-top"><div><div class="brand">Performance Intelligence &middot; Regular</div><h1 class="title">Regular Admissions &amp; Forms Tracker</h1></div><div class="header-meta"><div class="badge">Live Report</div><div class="date">{MONTH_LABEL} &middot; FTD {report_date.strftime('%d %b')}</div></div></div></header>
<div class="tabs"><label class="tab-label" for="tab-admissions">&#x1f393; Admissions</label><label class="tab-label" for="tab-forms">&#x1f4cb; Forms</label><label class="tab-label" for="tab-amity-forms">&#x1f4ca; Amity Forms YoY</label><label class="tab-label" for="tab-amity-adm">&#x1f4ca; Amity Adm YoY</label></div>

<section class="panel" id="panel-admissions">
<div class="kpis">
<div class="kpi"><div class="kpi-label">YTD Total</div><div class="kpi-value">{n(admt['YTD Ach'])}</div><div class="kpi-sub">Admissions</div></div>
<div class="kpi"><div class="kpi-label">{MONTH_SHORT} Ach %</div><div class="kpi-value {pc(admt.iloc[4])}">{pv(admt.iloc[4]):.1f}%</div><div class="kpi-ratio" style="font-size:11px;opacity:0.8">{admt.iloc[3]}/{admt.iloc[2]}</div></div>
<div class="kpi"><div class="kpi-label">Week Ach %</div><div class="kpi-value {pc(admt.iloc[7])}">{pv(admt.iloc[7]):.1f}%</div><div class="kpi-ratio" style="font-size:11px;opacity:0.8">{admt.iloc[6]}/{admt.iloc[5]}</div></div>
</div>
<div class="section-label"><span>College-wise Breakdown &middot; YTD from 1 Jan {report_date.year}</span></div>
<div class="table-wrap"><table><thead><tr><th rowspan="2" style="width:30%;vertical-align:middle;border-right:1px solid rgba(255,255,255,.12)">College</th><th rowspan="2" style="vertical-align:middle;color:rgba(255,255,255,.9);border-right:1px solid rgba(255,255,255,.12)">YTD</th><th colspan="3" class="th-group">{MONTH_SHORT}</th><th colspan="3" class="th-group">Week</th><th colspan="3" class="th-group">FTD</th></tr><tr><th>Target</th><th>Ach</th><th>Ach %</th><th>Target</th><th>Ach</th><th>Ach %</th><th>Target</th><th>Ach</th><th>Ach %</th></tr></thead><tbody>{rows(adm)}</tbody></table></div>
<div class="legend"><div class="legend-item"><span class="dot green"></span> &ge; 100% &mdash; Exceeding</div><div class="legend-item"><span class="dot amber"></span> 70&ndash;99% &mdash; On Track</div><div class="legend-item"><span class="dot red"></span> &lt; 70% &mdash; Needs Attention</div></div>
</section>

<section class="panel" id="panel-forms">
<div class="kpis">
<div class="kpi"><div class="kpi-label">YTD Total</div><div class="kpi-value">{n(formt['YTD Ach'])}</div><div class="kpi-sub">Forms</div></div>
<div class="kpi"><div class="kpi-label">{MONTH_SHORT} Ach %</div><div class="kpi-value {pc(formt.iloc[4])}">{pv(formt.iloc[4]):.1f}%</div><div class="kpi-ratio" style="font-size:11px;opacity:0.8">{formt.iloc[3]}/{formt.iloc[2]}</div></div>
<div class="kpi"><div class="kpi-label">Week Ach %</div><div class="kpi-value {pc(formt.iloc[7])}">{pv(formt.iloc[7]):.1f}%</div><div class="kpi-ratio" style="font-size:11px;opacity:0.8">{formt.iloc[6]}/{formt.iloc[5]}</div></div>
</div>
<div class="section-label"><span>College-wise Breakdown &middot; YTD from 1 Jan {report_date.year}</span></div>
<div class="table-wrap"><table><thead><tr><th rowspan="2" style="width:30%;vertical-align:middle;border-right:1px solid rgba(255,255,255,.12)">College</th><th rowspan="2" style="vertical-align:middle;color:rgba(255,255,255,.9);border-right:1px solid rgba(255,255,255,.12)">YTD</th><th colspan="3" class="th-group">{MONTH_SHORT}</th><th colspan="3" class="th-group">Week</th><th colspan="3" class="th-group">FTD</th></tr><tr><th>Target</th><th>Ach</th><th>Ach %</th><th>Target</th><th>Ach</th><th>Ach %</th><th>Target</th><th>Ach</th><th>Ach %</th></tr></thead><tbody>{rows(forms)}</tbody></table></div>
<div class="legend"><div class="legend-item"><span class="dot green"></span> &ge; 100% &mdash; Exceeding</div><div class="legend-item"><span class="dot amber"></span> 70&ndash;99% &mdash; On Track</div><div class="legend-item"><span class="dot red"></span> &lt; 70% &mdash; Needs Attention</div></div>
</section>

<section class="panel" id="panel-amity-forms">
{_amity_yoy_html_section(amity_yoy_df)}
</section>

<section class="panel" id="panel-amity-adm">
{_amity_adm_yoy_html_section(amity_adm_df)}
</section>

<footer class="footer"><div class="footer-note">Percentages show achievement vs target. Ratios shown in KPIs. FTD = For The Day.</div><div class="footer-brand">Regular &middot; Performance &middot; {MONTH_LABEL}</div></footer>
</div></body></html>'''

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


def send_via_whapi(file_path, caption):
    """Send a file to the WhatsApp Admin group via WHAPI (best-effort).
    On any failure the file is copied to LOCAL_FALLBACK_DIR."""
    if not WHAPI_TOKEN:
        print(f"  ⚠️  WHAPI_TOKEN not set — file already at: {file_path}")
        _save_fallback(file_path)
        return False

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

    payload = {'to': WHATSAPP_GROUP, 'media': media_data, 'caption': caption}
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

async def screenshot_html_tabs(html_path, tab_ids, png_paths, viewport_width=1024):
    """Screenshot a tabbed HTML page once per tab by clicking each tab label.
    tab_ids: list of the `for` attr values on each <label> (e.g. ['t1','t2',...]).
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
                page = await browser.new_page(viewport={'width': viewport_width, 'height': 900})
                await page.goto(f'file:///{os.path.abspath(html_path)}', wait_until='networkidle', timeout=30000)
                await page.click(f'label[for="{tab_id}"]')
                await page.wait_for_timeout(400)
                await page.locator('.table-wrap:visible').first.screenshot(path=png_path)
                await page.close()
                print(f"  ✅ Screenshot saved: {os.path.basename(png_path)}")
                results.append(True)
            await browser.close()
    except Exception as e:
        print(f"  ⚠️  Screenshot failed: {e}")
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
        df_couns, df_adm, df_form = await online_get_data()
        print("  [1b] Preparing online LMS data...")
        online_sheets = online_prepare_data(df_couns, df_adm, df_form)
        print("  [1c] Generating online LMS HTML report...")
        online_html, online_summary = online_generate_html(online_sheets)
        print("  ✅ STEP 1 complete — Online LMS report generated")
        print()
    except Exception as e:
        print(f"  ❌ STEP 1 FAILED: {e}")
        import traceback
        traceback.print_exc(file=sys.stdout)
        online_html = None

    # ── STEP 1d: Screenshot Online (4 tabs) ───────────────────────────────
    online_pngs = []
    if online_html:
        print("  [1d] Taking screenshots of Online LMS tabs...")
        _online_tab_ids   = ['t1',        't4']
        _online_tab_names = ['Overview', 'Colleges']
        online_pngs = [
            os.path.join(OUTPUT_DIR, f'Online_LMS_{name}_{RUN_STAMP}.png')
            for name in _online_tab_names
        ]
        online_png_ok = await screenshot_html_tabs(online_html, _online_tab_ids, online_pngs)
        online_pngs = [p for p, ok in zip(online_pngs, online_png_ok) if ok]

    # ── STEP 2: Regular LMS ────────────────────────────────────────────────
    print("─── STEP 2/3: Regular LMS Report ───────────────────────────────────")
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

    # ── STEP 2d: Screenshot Regular (2 tabs) ──────────────────────────────
    regular_pngs = []
    if regular_html:
        print("  [2d] Taking screenshots of Regular LMS tabs...")
        _reg_tab_ids   = ['tab-admissions', 'tab-forms', 'tab-amity-forms', 'tab-amity-adm']
        _reg_tab_names = ['Admissions',     'Forms',     'Amity_Forms',      'Amity_Adm']
        regular_pngs = [
            os.path.join(OUTPUT_DIR, f'Regular_LMS_{name}_{RUN_STAMP}.png')
            for name in _reg_tab_names
        ]
        reg_png_ok = await screenshot_html_tabs(regular_html, _reg_tab_ids, regular_pngs)
        regular_pngs = [p for p, ok in zip(regular_pngs, reg_png_ok) if ok]

    # ── STEP 3: Send Screenshots via WhatsApp ─────────────────────────────
    print("─── STEP 3/3: Sending Screenshots to WhatsApp + Logging ────────────")
    _tab_labels = {
        'Online_LMS_Overview':    'Owner wise Achievement Report - Online Business',
        'Online_LMS_Colleges':    'Online LOB - University wise Forms & Adm',
        'Regular_LMS_Admissions':  'Admission Target vs Achieved',
        'Regular_LMS_Forms':       'Form Target vs Achieved',
        'Regular_LMS_Amity_Forms': 'Amity Total Forms - Campus YoY',
        'Regular_LMS_Amity_Adm':   'Amity Admissions - Campus YoY',
    }
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
            sent = send_via_whapi(png_path, f"{cap} — {FTD_DATE}")
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
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                    removed += 1
            except Exception as e:
                print(f"   ⚠️  Could not remove: {item} — {e}")
        print(f"   Cleaned: {removed} items removed from {OUTPUT_DIR}")

    print()
    print("=" * 60)
    print("📊 ALL REPORTS GENERATED SUCCESSFULLY")
    print("=" * 60)
    print(f"   FTD: {FTD_DATE}")
    print(f"   Online screenshots:  {'✅' if online_pngs else '❌'} ({len(online_pngs)}/2)")
    print(f"   Regular screenshots: {'✅' if regular_pngs else '❌'} ({len(regular_pngs)}/4)")
    print(f"   WHAPI sends: {'skipped (--local mode)' if LOCAL_MODE else ('attempted' if WHAPI_TOKEN else 'skipped (no token)')}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
