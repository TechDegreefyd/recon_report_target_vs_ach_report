#!/usr/bin/env python3
"""
Call Extension Report — Greeter IVR numbers x LMS Application/Admission funnel.

Numbers covered (Normal IVR group, per user):
  919484958351  CU LPU Combined   — College Specific
  919484958352  Amity             — College Specific
  919484958353  CU Online         — College Specific
  919484958355  DSA               — Generic
(956/954 numbers are intentionally excluded.)

Pipeline:
  1. Greeter (admin_call_log_list, session-scoped per DID) → Total Calls, Answered,
     Unique Calls, Unique Calls Answered — for FTD (today) and MTD (month to date).
  2. LMS DBs (Online / Regular / Amity) → for each number, find students where
     source ILIKE '%ivr%' AND first_source_url = <number>. Leads = distinct such
     students created within the window. App/Adm = how many of those students'
     course journeys hit Application / Admission status within the window.
     Summed across all 3 DBs (a number's leads can land in more than one DB).
  3. L2F% = App / Leads (lead-to-form), L2A% = Adm / Leads (lead-to-admission) — in that order.
  4. Screenshot the HTML (Playwright) and send it to WhatsApp via WHAPI.

Usage:
    python generate_ivr_extension_report.py             # build HTML, screenshot, send to WhatsApp
    python generate_ivr_extension_report.py --local      # build + screenshot only, skip WhatsApp send
    python generate_ivr_extension_report.py --nocleanup  # skip auto-cleanup of old report files
    python generate_ivr_extension_report.py --group <id> # override destination WhatsApp group id
"""
import os
import re
import sys
import time
import html
import base64
import asyncio
import argparse
import requests
import asyncpg
from datetime import datetime, timedelta, UTC
from dotenv import load_dotenv

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_DIR, '.env'))

parser = argparse.ArgumentParser()
parser.add_argument('--local', action='store_true', help='Skip WhatsApp send — build HTML + screenshot only')
parser.add_argument('--nocleanup', action='store_true', help='Skip auto-cleanup of old report files')
parser.add_argument('--group', default=None, help='Override destination WhatsApp group id')
args = parser.parse_args()

WHAPI_TOKEN = os.getenv('WHAPI_TOKEN_PAID')
WHATSAPP_GROUP_REGULAR_LMS = args.group or os.getenv('WHATSAPP_GROUP_REGULAR_LMS')

OUTPUT_DIR = os.path.join(_DIR, 'Automation Cron Job', 'IVR Extension Report')
os.makedirs(OUTPUT_DIR, exist_ok=True)

now_ist = datetime.now(UTC) + timedelta(hours=5, minutes=30)
FTD_DATE   = now_ist.strftime('%Y-%m-%d')
MTD_START  = now_ist.replace(day=1).strftime('%Y-%m-%d')
DATE_LABEL = now_ist.strftime('%d %B %Y')
RUN_STAMP  = now_ist.strftime('%Y%m%d_%H%M')

# ─── Number → label / group / DB routing ────────────────────────────────────────
NUMBERS = {
    '919484958351': {'label': 'CU LPU Combined',  'group': 'Specific'},
    '919484958352': {'label': 'Amity',            'group': 'Specific'},
    '919484958353': {'label': 'CU Online',        'group': 'Specific'},
    '919484958355': {'label': 'DSA',              'group': 'Generic'},
}
SPECIFIC_ORDER = ['919484958351', '919484958352', '919484958353']
GENERIC_ORDER  = ['919484958355']

# Each number's Leads/App/Adm come from exactly ONE dedicated LMS DB — per the
# reference queries in new_report_queries.txt (no cross-DB summing).
LMS_DB_CONFIGS = {
    'ONLINE': {
        'host': os.getenv('ONLINE_LMS_DB_HOST'), 'port': int(os.getenv('ONLINE_LMS_DB_PORT', '54321')),
        'database': os.getenv('ONLINE_LMS_DB_NAME'), 'user': os.getenv('ONLINE_LMS_DB_USER'),
        'password': os.getenv('ONLINE_LMS_DB_PASSWORD'),
    },
    'REGULAR': {
        'host': os.getenv('REGULAR_LMS_DB_HOST'), 'port': int(os.getenv('REGULAR_LMS_DB_PORT', '54321')),
        'database': os.getenv('REGULAR_LMS_DB_NAME'), 'user': os.getenv('REGULAR_LMS_DB_USER'),
        'password': os.getenv('REGULAR_LMS_DB_PASSWORD'),
    },
    'AMITY': {
        'host': os.getenv('REGULAR_AMITY_LMS_DB_HOST'), 'port': int(os.getenv('REGULAR_AMITY_LMS_DB_PORT', '54321')),
        'database': os.getenv('REGULAR_AMITY_LMS_DB_NAME'), 'user': os.getenv('REGULAR_AMITY_LMS_DB_USER'),
        'password': os.getenv('REGULAR_AMITY_LMS_DB_PASSWORD'),
    },
}

# number -> (db_name, style)
#   style 'broad'  = the wide Form Submitted/Walkin/... status list (regular + amity)
#   style 'online' = Application status only, used for the Online DB
NUMBER_DB_ROUTE = {
    '919484958351': ('REGULAR', 'broad'),   # CU LPU Combined
    '919484958355': ('REGULAR', 'broad'),   # DSA
    '919484958352': ('AMITY',   'broad'),   # Amity — no fee_type exclusion on admissions
    '919484958353': ('ONLINE',  'online'),  # CU Online
}

_FORM_STATUSES_BROAD = (
    "'Form Submitted – Portal Pending', 'Form Submitted – Completed', "
    "'Walkin Completed', 'Walkin Marked', 'Exam/Interview Scheduled', "
    "'Offer Letter/Results Pending', 'Offer Letter/Results Released', 'Ready For Admission'"
)
_FORM_STATUSES_ONLINE = "'Application'"
_ADM_FEE_EXCLUDE = "AND csj.fee_type NOT IN ('Partial Done', 'Partial Paid', 'Partially Paid')"


def log(msg):
    print(f'[{time.perf_counter():7.1f}s] {msg}', flush=True)


# ═══════════════════════════════════════════════════════════════════════════════
# PART 1 — GREETER CALL LOG (per number, per window)
# ═══════════════════════════════════════════════════════════════════════════════

GREETER_USERNAME = os.getenv('GREETER_USERNAME_IVR')
GREETER_PASSWORD = os.getenv('GREETER_USERNAME_PASSWORD_IVR')
BASE_URL          = 'https://greeter.co.in/'
LOGIN_URL         = BASE_URL + 'login'
PLAN_DETAILS_URL  = BASE_URL + 'getAdminAllPlanDetails'
CHANGE_PLAN_URL   = BASE_URL + 'adminUserChangePlanData/{}'
CALL_LOG_URL      = BASE_URL + 'admin_call_log_list'
CALL_LOG_PAGE     = BASE_URL + 'admin/call_log'


def _extract_csrf(html_text):
    for line in html_text.splitlines():
        if 'csrf' in line.lower() and 'value' in line.lower():
            m = re.search(r'value=["\']([^"\']{20,})["\']', line)
            if m:
                return m.group(1)
    return ''


def _extract_csrf_meta(html_text):
    m = re.search(r'name="csrf-token"\s+content="([^"]+)"', html_text)
    return m.group(1) if m else ''


def _strip_tags(s):
    return re.sub(r'<[^>]+>', '', s or '').strip()


def greeter_login():
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0'})

    r = session.get(LOGIN_URL, timeout=30)
    csrf = _extract_csrf(r.text)
    payload = {'username': GREETER_USERNAME, 'password': GREETER_PASSWORD}
    if csrf:
        payload['csrfmiddlewaretoken'] = csrf
        session.headers.update({'Referer': LOGIN_URL})
    r = session.post(LOGIN_URL, data=payload, timeout=30, allow_redirects=True)
    if 'login' in r.url.lower():
        raise SystemExit('Greeter login failed — check GREETER_USERNAME_IVR / GREETER_USERNAME_PASSWORD_IVR.')

    r = session.get(CALL_LOG_PAGE, timeout=30)
    csrf_token = _extract_csrf_meta(r.text)
    session.headers.update({
        'X-CSRF-TOKEN': csrf_token,
        'Referer': CALL_LOG_PAGE,
        'X-Requested-With': 'XMLHttpRequest',
    })
    return session


def greeter_fetch_rows(session, plan_id, filter_name):
    """filter_name: 'today_search' or 'month_search'. Returns list of parsed call rows."""
    session.get(CHANGE_PLAN_URL.format(plan_id), timeout=30)

    all_rows, start, total = [], 0, None
    while True:
        params = {
            'sEcho': 1, 'iColumns': 14, 'iDisplayStart': start, 'iDisplayLength': 1000,
            'sSearch': '', 'customer_number': '', 'agent_number': '', 'any_number': '',
            'Answered': 'false', 'NoAnswered': 'false', 'AFTHRS': 'false', 'Ivr': 'false',
            'VoiceMail': 'false', 'CdrLogArchive': 'false',
            filter_name: 1, 'custom_search': 'Custom Range', 'callType': '', 'agent_name_filter': '',
            '_': int(time.time() * 1000),
        }
        r = session.get(CALL_LOG_URL, params=params, timeout=60)
        data = r.json()
        if total is None:
            total = int(data.get('iTotalDisplayRecords', 0))
        raw_rows = data.get('aaData', [])
        for rr in raw_rows:
            caller_html = rr.get('4', '') or ''
            status_html = rr.get('12', '') or ''
            m = re.search(r'data-customernumber="(\d*)"', caller_html)
            caller = m.group(1) if m else _strip_tags(caller_html)
            all_rows.append({'caller': caller, 'status': _strip_tags(status_html)})
        start += 1000
        if len(raw_rows) < 1000 or not total or len(all_rows) >= total:
            break
    return all_rows


def call_stats(rows):
    total = len(rows)
    answered = sum(1 for r in rows if r['status'] == 'Answered')
    callers = {}
    for r in rows:
        c = r['caller']
        if not c:
            continue
        callers.setdefault(c, False)
        if r['status'] == 'Answered':
            callers[c] = True
    unique_calls = len(callers)
    unique_answered = sum(1 for v in callers.values() if v)
    return {
        'total_calls': total,
        'answered': answered,
        'unique_calls': unique_calls,
        'unique_answered': unique_answered,
    }


def fetch_greeter_all():
    log('Greeter: logging in …')
    session = greeter_login()
    r = session.get(PLAN_DETAILS_URL, timeout=30)
    plans = r.json()['data']['getUserPlanDetails']
    plan_by_number = {str(p['mapp_number']): p['user_plan_id'] for p in plans}

    result = {}
    for number in NUMBERS:
        plan_id = plan_by_number.get(number)
        if not plan_id:
            log(f'  ⚠️  {number} not found on this Greeter account — skipping call stats')
            result[number] = {'ftd': call_stats([]), 'mtd': call_stats([])}
            continue
        log(f'  {number} ({NUMBERS[number]["label"]}) …')
        ftd_rows = greeter_fetch_rows(session, plan_id, 'today_search')
        mtd_rows = greeter_fetch_rows(session, plan_id, 'month_search')
        result[number] = {'ftd': call_stats(ftd_rows), 'mtd': call_stats(mtd_rows)}
        log(f'    FTD: {result[number]["ftd"]}  MTD: {result[number]["mtd"]}')
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# PART 2 — LMS DB (Application / Admission counts per number, per window)
# ═══════════════════════════════════════════════════════════════════════════════

def _bucket(dates, ftd_d, mtd_s, mtd_e):
    ftd = sum(1 for d in dates if d == ftd_d)
    mtd = sum(1 for d in dates if mtd_s <= d <= mtd_e)
    return ftd, mtd


async def db_fetch_number_funnel(conn, style, number, ftd_date, mtd_start, mtd_end):
    """
    Mirrors new_report_queries.txt: leads = distinct students whose FIRST-ever
    student_lead_activities row carries utm_campaign = <number>; App/Adm = whether
    that student's course journey (any course) reached the form/admission status
    set, keyed off that student's EARLIEST such event date.
    """
    form_status_clause = _FORM_STATUSES_ONLINE if style == 'online' else _FORM_STATUSES_BROAD
    adm_fee_clause = _ADM_FEE_EXCLUDE if style != 'amity' else ''

    leads_sql = """
        SELECT DISTINCT ON (sla.student_id)
            sla.student_id, (sla.created_at AT TIME ZONE 'Asia/Kolkata')::date AS d
        FROM student_lead_activities sla
        WHERE sla.utm_campaign = $1
        ORDER BY sla.student_id, sla.created_at ASC
    """
    lead_rows = await conn.fetch(leads_sql, number)
    student_ids = [r['student_id'] for r in lead_rows]

    ftd_d = datetime.strptime(ftd_date, '%Y-%m-%d').date()
    mtd_s = datetime.strptime(mtd_start, '%Y-%m-%d').date()
    mtd_e = datetime.strptime(mtd_end, '%Y-%m-%d').date()

    ftd_leads, mtd_leads = _bucket([r['d'] for r in lead_rows], ftd_d, mtd_s, mtd_e)

    if not student_ids:
        return {'ftd_leads': 0, 'mtd_leads': 0, 'ftd_app': 0, 'mtd_app': 0, 'ftd_adm': 0, 'mtd_adm': 0}

    form_sql = f"""
        SELECT csj.student_id, MIN(csj.created_at AT TIME ZONE 'Asia/Kolkata')::date AS d
        FROM course_status_journeys csj
        WHERE csj.student_id = ANY($1::text[])
          AND csj.course_status IN ({form_status_clause})
        GROUP BY csj.student_id
    """
    adm_sql = f"""
        SELECT csj.student_id, MIN(csj.created_at AT TIME ZONE 'Asia/Kolkata')::date AS d
        FROM course_status_journeys csj
        WHERE csj.student_id = ANY($1::text[])
          AND csj.course_status IN ('Admission', 'Enrolled')
          {adm_fee_clause}
        GROUP BY csj.student_id
    """
    form_rows = await conn.fetch(form_sql, student_ids)
    adm_rows = await conn.fetch(adm_sql, student_ids)

    ftd_app, mtd_app = _bucket([r['d'] for r in form_rows], ftd_d, mtd_s, mtd_e)
    ftd_adm, mtd_adm = _bucket([r['d'] for r in adm_rows], ftd_d, mtd_s, mtd_e)

    return {
        'ftd_leads': ftd_leads, 'mtd_leads': mtd_leads,
        'ftd_app': ftd_app, 'mtd_app': mtd_app,
        'ftd_adm': ftd_adm, 'mtd_adm': mtd_adm,
    }


async def fetch_lms_all():
    result = {n: {'ftd_app': 0, 'mtd_app': 0, 'ftd_adm': 0, 'mtd_adm': 0, 'ftd_leads': 0, 'mtd_leads': 0} for n in NUMBERS}
    open_conns = {}
    try:
        for number in NUMBERS:
            db_name, style = NUMBER_DB_ROUTE[number]
            cfg = LMS_DB_CONFIGS[db_name]
            if not cfg['host']:
                log(f'  ⚠️  {db_name} DB not configured — skipping {number}')
                continue
            if db_name not in open_conns:
                log(f'LMS DB [{db_name}]: connecting …')
                open_conns[db_name] = await asyncpg.connect(
                    host=cfg['host'], port=cfg['port'], database=cfg['database'],
                    user=cfg['user'], password=cfg['password'], timeout=30)
            conn = open_conns[db_name]
            result[number] = await db_fetch_number_funnel(conn, style, number, FTD_DATE, MTD_START, FTD_DATE)
            log(f'  {number} [{db_name}]: {result[number]}')
    finally:
        for conn in open_conns.values():
            await conn.close()
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# PART 3 — Combine + build report rows
# ═══════════════════════════════════════════════════════════════════════════════

def build_row(number, call_data, lms_data, window):
    c = call_data[number][window]
    d = lms_data[number]
    prefix = 'ftd' if window == 'ftd' else 'mtd'
    app = d[f'{prefix}_app']
    adm = d[f'{prefix}_adm']
    leads = d[f'{prefix}_leads']
    l2f = round(app / leads * 100, 1) if leads else 0.0
    l2a = round(adm / leads * 100, 1) if leads else 0.0
    return {
        'label': NUMBERS[number]['label'],
        'number': number,
        'total_calls': c['total_calls'],
        'answered': c['answered'],
        'unique_calls': c['unique_calls'],
        'unique_answered': c['unique_answered'],
        'leads': leads,
        'app': app,
        'adm': adm,
        'l2f': l2f,
        'l2a': l2a,
    }


def sum_rows(rows):
    keys = ['total_calls', 'answered', 'unique_calls', 'unique_answered', 'leads', 'app', 'adm']
    out = {k: sum(r[k] for r in rows) for k in keys}
    out['l2f'] = round(out['app'] / out['leads'] * 100, 1) if out['leads'] else 0.0
    out['l2a'] = round(out['adm'] / out['leads'] * 100, 1) if out['leads'] else 0.0
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# PART 4 — HTML
# ═══════════════════════════════════════════════════════════════════════════════

def esc(v):
    return html.escape(str(v))


def build_html(call_data, lms_data):
    def section(window, title):
        specific_rows = [build_row(n, call_data, lms_data, window) for n in SPECIFIC_ORDER]
        generic_rows  = [build_row(n, call_data, lms_data, window) for n in GENERIC_ORDER]
        specific_tot  = sum_rows(specific_rows)
        generic_tot   = sum_rows(generic_rows)
        grand_tot     = sum_rows(specific_rows + generic_rows)

        def pct_chip(v):
            cls = 'cg' if v >= 20 else ('co' if v >= 10 else 'cr')
            return f'<span class="chip {cls}">{v}%</span>'

        def type_row(label, tot, extra_class='', numbers=None):
            badges = ''.join(f'<span class="num-badge">{n[-2:]}</span>' for n in (numbers or []))
            return f'''<tr class="type-row {extra_class}">
              <td class="rowlabel" colspan="2"><span class="rowlabel-inner">{label} {badges}</span></td>
              <td>{tot["total_calls"]}</td>
              <td>{tot["answered"]}</td>
              <td>{tot["unique_calls"]}</td>
              <td>{tot["unique_answered"]}</td>
              <td class="leads-cell">{tot["leads"]}</td>
              <td>{tot["app"]}</td>
              <td class="adm-cell">{tot["adm"]}</td>
              <td>{pct_chip(tot["l2f"])}</td>
              <td>{pct_chip(tot["l2a"])}</td>
            </tr>'''

        def college_row(r):
            return f'''<tr class="college-row">
              <td></td>
              <td class="rowlabel"><span class="rowlabel-inner">{esc(r["label"])} <span class="num-badge">{r["number"][-2:]}</span></span></td>
              <td>{r["total_calls"]}</td>
              <td>{r["answered"]}</td>
              <td>{r["unique_calls"]}</td>
              <td>{r["unique_answered"]}</td>
              <td class="leads-cell">{r["leads"]}</td>
              <td>{r["app"]}</td>
              <td class="adm-cell">{r["adm"]}</td>
              <td>{pct_chip(r["l2f"])}</td>
              <td>{pct_chip(r["l2a"])}</td>
            </tr>'''

        rows_html  = type_row('Generic', generic_tot, 'generic', GENERIC_ORDER)
        rows_html += type_row('Specific', specific_tot, 'specific')
        rows_html += ''.join(college_row(r) for r in specific_rows)
        rows_html += f'''<tr class="grand-row">
          <td class="rowlabel" colspan="2">Grand Total</td>
          <td>{grand_tot["total_calls"]}</td>
          <td>{grand_tot["answered"]}</td>
          <td>{grand_tot["unique_calls"]}</td>
          <td>{grand_tot["unique_answered"]}</td>
          <td class="leads-cell">{grand_tot["leads"]}</td>
          <td>{grand_tot["app"]}</td>
          <td class="adm-cell">{grand_tot["adm"]}</td>
          <td>{pct_chip(grand_tot["l2f"])}</td>
          <td>{pct_chip(grand_tot["l2a"])}</td>
        </tr>'''

        return f'''
        <div class="tcard">
          <div class="tcard-head">
            <span class="tcard-title">{title}</span>
            <span class="tcard-sub">{DATE_LABEL if window == 'ftd' else MTD_START + ' – ' + FTD_DATE}</span>
          </div>
          <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th style="text-align:left" colspan="2">Type</th>
                <th colspan="2" class="grp-calls">Calls</th>
                <th colspan="2" class="grp-unique">Unique Calls</th>
                <th class="grp-leads">Leads</th>
                <th colspan="2" class="grp-funnel">Funnel</th>
                <th colspan="2" class="grp-l2a">Conversion</th>
              </tr>
              <tr>
                <th style="text-align:left" colspan="2"></th>
                <th>Total</th>
                <th>Answered</th>
                <th>Total</th>
                <th>Answered</th>
                <th></th>
                <th>App</th>
                <th>Adm</th>
                <th>L2F%</th>
                <th>L2A%</th>
              </tr>
            </thead>
            <tbody>{rows_html}</tbody>
          </table>
          </div>
        </div>'''

    ftd_specific = [build_row(n, call_data, lms_data, 'ftd') for n in SPECIFIC_ORDER]
    ftd_generic  = [build_row(n, call_data, lms_data, 'ftd') for n in GENERIC_ORDER]
    ftd_grand    = sum_rows(ftd_specific + ftd_generic)

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Call Extension Report · {DATE_LABEL}</title>
<style>
  @font-face {{
    font-family: 'Inter';
    src: local('Inter');
    font-weight: 400 800;
  }}
  :root {{
    --bg: #F8F9FB; --surface: #fff; --border: #e5e7eb; --border-soft: #f3f4f6; --table-border: #cbd1db;
    --thead-bg: #F5F6F8; --text: #111827; --text-dim: #4b5563; --text-mid: #1f2937; --text-soft: #374151;
    --pill-bg: #eef2ff; --pill-text: #4338ca; --pill-border: #c7d2fe;
    --grand-bg: #111827; --grand-text: #fff;
    --leads-color: #92400e; --adm-color: #15803d;
    --chip-g-bg: #f0fdf4; --chip-g-text: #15803d; --chip-g-border: #bbf7d0;
    --chip-o-bg: #fff7ed; --chip-o-text: #c2410c; --chip-o-border: #fed7aa;
    --chip-r-bg: #fef2f2; --chip-r-text: #dc2626; --chip-r-border: #fecaca;
    --typerow-bg: #fafafa;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0f1117; --surface: #171a23; --border: #2a2e3a; --border-soft: #23262f; --table-border: #3c4152;
      --thead-bg: #1c1f29; --text: #e8e9ee; --text-dim: #9298a8; --text-mid: #dcdee5; --text-soft: #c2c5d0;
      --pill-bg: #232a4d; --pill-text: #a5b4fc; --pill-border: #363f6e;
      --grand-bg: #05060a; --grand-text: #f4f5f8;
      --leads-color: #f0b054; --adm-color: #4ade80;
      --chip-g-bg: #10261a; --chip-g-text: #4ade80; --chip-g-border: #1f4a30;
      --chip-o-bg: #2b1c0e; --chip-o-text: #fb923c; --chip-o-border: #4a3018;
      --chip-r-bg: #2b1414; --chip-r-text: #f87171; --chip-r-border: #4a1f1f;
      --typerow-bg: #1a1d27;
    }}
  }}
  :root[data-theme="dark"] {{
    --bg: #0f1117; --surface: #171a23; --border: #2a2e3a; --border-soft: #23262f; --table-border: #3c4152;
    --thead-bg: #1c1f29; --text: #e8e9ee; --text-dim: #9298a8; --text-mid: #dcdee5; --text-soft: #c2c5d0;
    --pill-bg: #232a4d; --pill-text: #a5b4fc; --pill-border: #363f6e;
    --grand-bg: #05060a; --grand-text: #f4f5f8;
    --leads-color: #f0b054; --adm-color: #4ade80;
    --chip-g-bg: #10261a; --chip-g-text: #4ade80; --chip-g-border: #1f4a30;
    --chip-o-bg: #2b1c0e; --chip-o-text: #fb923c; --chip-o-border: #4a3018;
    --chip-r-bg: #2b1414; --chip-r-text: #f87171; --chip-r-border: #4a1f1f;
    --typerow-bg: #1a1d27;
  }}
  :root[data-theme="light"] {{
    --bg: #F8F9FB; --surface: #fff; --border: #e5e7eb; --border-soft: #f3f4f6; --table-border: #cbd1db;
    --thead-bg: #F5F6F8; --text: #111827; --text-dim: #4b5563; --text-mid: #1f2937; --text-soft: #374151;
    --pill-bg: #eef2ff; --pill-text: #4338ca; --pill-border: #c7d2fe;
    --grand-bg: #111827; --grand-text: #fff;
    --leads-color: #92400e; --adm-color: #15803d;
    --chip-g-bg: #f0fdf4; --chip-g-text: #15803d; --chip-g-border: #bbf7d0;
    --chip-o-bg: #fff7ed; --chip-o-text: #c2410c; --chip-o-border: #fed7aa;
    --chip-r-bg: #fef2f2; --chip-r-text: #dc2626; --chip-r-border: #fecaca;
    --typerow-bg: #fafafa;
  }}

  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Inter', system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 36px 40px 48px;
    font-size: 15px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }}
  .topbar {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 28px; }}
  .topbar-left {{ display: flex; align-items: center; gap: 14px; }}
  .logo-mark {{
    width: 40px; height: 40px; border-radius: 10px;
    background: linear-gradient(135deg,#4338ca,#7c3aed);
    display: flex; align-items: center; justify-content: center;
    font-size: 13px; font-weight: 800; color: #fff; letter-spacing: -0.5px;
  }}
  .topbar h1 {{ font-size: 1.15rem; font-weight: 700; color: var(--text); }}
  .topbar p  {{ font-size: .85rem; color: var(--text-dim); margin-top: 2px; }}
  .pill {{
    font-size: .72rem; font-weight: 700; letter-spacing: .8px; text-transform: uppercase;
    padding: 6px 14px; border-radius: 999px;
    background: var(--pill-bg); color: var(--pill-text); border: 1px solid var(--pill-border);
  }}
  .kpis {{ display: grid; grid-template-columns: repeat(5,1fr); gap: 10px; margin-bottom: 28px; }}
  .kpi-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; position: relative; overflow: hidden; }}
  .kpi-card::after {{ content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px; background: var(--ac, var(--border)); border-radius: 12px 12px 0 0; }}
  .kpi-card .v {{ font-size: 1.7rem; font-weight: 800; color: var(--vc, var(--text)); line-height: 1; margin-top: 4px; font-variant-numeric: tabular-nums; }}
  .kpi-card .l {{ font-size: .7rem; font-weight: 600; color: var(--text-dim); text-transform: uppercase; letter-spacing: .6px; margin-top: 8px; }}

  .tcard {{ background: var(--surface); border: 1px solid var(--table-border); border-radius: 14px; overflow: hidden; margin-bottom: 24px; }}
  .tcard-head {{ padding: 14px 18px 13px; border-bottom: 1px solid var(--border-soft); display: flex; align-items: center; justify-content: space-between; }}
  .tcard-title {{ font-size: .82rem; font-weight: 700; text-transform: uppercase; letter-spacing: .7px; color: var(--text); }}
  .tcard-sub {{ font-size: .8rem; color: var(--text-dim); }}

  .table-scroll {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; min-width: 720px; }}
  thead tr:first-child th {{
    padding: 7px 10px; font-size: .68rem; font-weight: 800; text-transform: uppercase; letter-spacing: .5px;
    text-align: center; background: var(--thead-bg);
    border-bottom: 1px solid var(--table-border); border-right: 1px solid var(--table-border);
  }}
  thead tr:first-child th:last-child {{ border-right: none; }}
  thead tr:last-child th {{
    padding: 6px 10px; font-size: .74rem; font-weight: 700; text-transform: uppercase; letter-spacing: .5px;
    color: var(--text-dim); text-align: right; white-space: nowrap; background: var(--thead-bg);
    border-bottom: 2px solid var(--table-border); border-right: 1px solid var(--table-border);
  }}
  thead tr:last-child th:last-child {{ border-right: none; }}
  .grp-calls   {{ background: #eff6ff; color: #1d4ed8; }}
  .grp-unique  {{ background: #fdf4ff; color: #a21caf; }}
  .grp-leads   {{ background: #fffbeb; color: #92400e; }}
  .grp-funnel  {{ background: #f0fdf4; color: #15803d; }}
  .grp-l2a     {{ background: #eef2ff; color: #4338ca; }}
  @media (prefers-color-scheme: dark) {{
    .grp-calls  {{ background: #16233d; color: #93c5fd; }}
    .grp-unique {{ background: #351d3d; color: #e9a8ea; }}
    .grp-leads  {{ background: #2b2311; color: #f0c674; }}
    .grp-funnel {{ background: #122a1a; color: #6ee7a0; }}
    .grp-l2a    {{ background: #202449; color: #b3bbfa; }}
  }}
  :root[data-theme="dark"] .grp-calls  {{ background: #16233d; color: #93c5fd; }}
  :root[data-theme="dark"] .grp-unique {{ background: #351d3d; color: #e9a8ea; }}
  :root[data-theme="dark"] .grp-leads  {{ background: #2b2311; color: #f0c674; }}
  :root[data-theme="dark"] .grp-funnel {{ background: #122a1a; color: #6ee7a0; }}
  :root[data-theme="dark"] .grp-l2a    {{ background: #202449; color: #b3bbfa; }}

  tbody td {{
    padding: 9px 10px; text-align: right; font-size: .9rem; font-weight: 700; color: var(--text-mid);
    border-bottom: 1px solid var(--table-border); border-right: 1px solid var(--table-border);
    font-variant-numeric: tabular-nums;
  }}
  tbody td:last-child {{ border-right: none; }}
  tbody tr:last-child td {{ border-bottom: none; }}
  .rowlabel {{ text-align: left !important; font-weight: 700; }}
  .rowlabel-inner {{ display: inline-flex; align-items: center; gap: 8px; }}
  tr.type-row td {{ background: var(--typerow-bg); font-weight: 800; }}
  tr.type-row.generic  .rowlabel {{ color: #b45309; }}
  tr.type-row.specific .rowlabel {{ color: #4338ca; }}
  @media (prefers-color-scheme: dark) {{ tr.type-row.generic .rowlabel {{ color: #f0b054; }} tr.type-row.specific .rowlabel {{ color: #a5b4fc; }} }}
  :root[data-theme="dark"] tr.type-row.generic .rowlabel {{ color: #f0b054; }}
  :root[data-theme="dark"] tr.type-row.specific .rowlabel {{ color: #a5b4fc; }}
  tr.college-row .rowlabel {{ padding-left: 28px; font-weight: 600; color: var(--text-soft); }}
  .num-badge {{
    display: inline-flex; align-items: center; justify-content: center;
    min-width: 26px; height: 20px; font-size: .68rem; font-weight: 800;
    color: var(--pill-text); background: var(--pill-bg);
    border: 1px solid var(--pill-border); border-radius: 5px; padding: 0 6px;
    font-variant-numeric: tabular-nums;
  }}
  tr.grand-row td {{ background: var(--grand-bg); color: var(--grand-text); font-weight: 800; border-top: 2px solid var(--grand-bg); border-right-color: rgba(255,255,255,.12); }}
  tr.grand-row td:last-child {{ border-right: none; }}
  .leads-cell {{ color: var(--leads-color) !important; }}
  .adm-cell   {{ color: var(--adm-color) !important; }}
  tr.grand-row .leads-cell, tr.grand-row .adm-cell {{ color: var(--grand-text) !important; }}

  .chip {{ display: inline-block; padding: 3px 9px; border-radius: 5px; font-size: .8rem; font-weight: 800; }}
  .cg {{ background: var(--chip-g-bg); color: var(--chip-g-text); border: 1px solid var(--chip-g-border); }}
  .co {{ background: var(--chip-o-bg); color: var(--chip-o-text); border: 1px solid var(--chip-o-border); }}
  .cr {{ background: var(--chip-r-bg); color: var(--chip-r-text); border: 1px solid var(--chip-r-border); }}
  tr.grand-row .chip {{ background: rgba(255,255,255,.15); color: #fff; border: 1px solid rgba(255,255,255,.25); }}

  footer {{ text-align: center; margin-top: 8px; font-size: .75rem; color: var(--text-dim); }}

  @media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; animation: none !important; }} }}
</style>
</head>
<body>

<div class="shell">

<div class="topbar">
  <div class="topbar-left">
    <div class="logo-mark">CE</div>
    <div>
      <h1>Call Extension Report</h1>
      <p>{DATE_LABEL} &nbsp;·&nbsp; Normal IVR &nbsp;·&nbsp; {len(NUMBERS)} numbers tracked</p>
    </div>
  </div>
  <span class="pill">greeter.co.in</span>
</div>

<div class="kpis">
  <div class="kpi-card" style="--ac:#3b82f6;--vc:#2563eb"><div class="v">{ftd_grand["total_calls"]}</div><div class="l">Total Calls (FTD)</div></div>
  <div class="kpi-card" style="--ac:#22c55e;--vc:#16a34a"><div class="v">{ftd_grand["answered"]}</div><div class="l">Answered (FTD)</div></div>
  <div class="kpi-card" style="--ac:#f59e0b;--vc:#b45309"><div class="v">{ftd_grand["leads"]}</div><div class="l">Leads (FTD)</div></div>
  <div class="kpi-card" style="--ac:#10b981;--vc:#059669"><div class="v">{ftd_grand["adm"]}</div><div class="l">Admissions (FTD)</div></div>
  <div class="kpi-card" style="--ac:#7c3aed;--vc:#6d28d9"><div class="v">{ftd_grand["l2a"]}%</div><div class="l">L2A % (FTD)</div></div>
</div>

{section('ftd', 'For The Day (FTD)')}
{section('mtd', 'Month To Date (MTD)')}

<footer>Source: greeter.co.in (call data) &nbsp;·&nbsp; LMS databases (App/Adm) &nbsp;·&nbsp; Generated {now_ist.strftime('%d %b %Y, %I:%M %p')} IST</footer>

</div>
</body>
</html>'''


# ═══════════════════════════════════════════════════════════════════════════════
# CLEANUP
# ═══════════════════════════════════════════════════════════════════════════════

def cleanup_old_files():
    """Delete every prior IVR Extension Report HTML file — each run keeps only its own output."""
    if not os.path.isdir(OUTPUT_DIR):
        return
    deleted = 0
    for fname in os.listdir(OUTPUT_DIR):
        if not fname.endswith(('.html', '.png')):
            continue
        fpath = os.path.join(OUTPUT_DIR, fname)
        try:
            os.remove(fpath)
            deleted += 1
        except Exception as e:
            log(f'  cleanup: could not remove {fpath}: {e}')
    if deleted:
        log(f'Cleanup: removed {deleted} old report file(s)')


# ═══════════════════════════════════════════════════════════════════════════════
# SCREENSHOT + WHATSAPP
# ═══════════════════════════════════════════════════════════════════════════════

async def screenshot_html(html_path, png_path, viewport_width=1400):
    """Full-page screenshot of the report's .shell wrapper (same pattern as
    generate_all_lms_reports.py's screenshot_html_tabs, minus the per-tab looping)."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log('  ⚠️  playwright not installed — run: pip install playwright && playwright install chromium')
        return False

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=['--no-sandbox', '--disable-setuid-sandbox'])
            page = await browser.new_page(viewport={'width': viewport_width, 'height': 900}, device_scale_factor=2)
            await page.goto(f'file:///{os.path.abspath(html_path)}', wait_until='networkidle', timeout=30000)
            await page.wait_for_timeout(300)
            await page.locator('.shell').screenshot(path=png_path)
            await browser.close()
        log(f'  ✅ Screenshot saved: {os.path.basename(png_path)}')
        return True
    except Exception as e:
        log(f'  ⚠️  Screenshot failed: {e}')
        return False


def send_via_whapi(file_path, caption, group_id):
    """Send a PNG to a WhatsApp group via WHAPI (best-effort)."""
    if not WHAPI_TOKEN:
        log('  ⚠️  WHAPI_TOKEN_PAID not set — skipping WhatsApp send')
        return False
    if not group_id:
        log('  ⚠️  No destination group id (WHATSAPP_GROUP_REGULAR_LMS not set) — skipping WhatsApp send')
        return False

    with open(file_path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode('utf-8')
    media_data = f'data:image/png;name={os.path.basename(file_path)};base64,{b64}'
    payload = {'to': group_id, 'media': media_data, 'caption': caption}
    headers = {'accept': 'application/json', 'authorization': f'Bearer {WHAPI_TOKEN}', 'content-type': 'application/json'}

    for attempt in range(2):
        try:
            r = requests.post('https://gate.whapi.cloud/messages/image', headers=headers, json=payload, timeout=20)
            if 200 <= r.status_code < 300:
                log(f'  ✅ WHAPI sent -> {group_id}')
                return True
            log(f'  ⚠️  WHAPI HTTP {r.status_code}: {r.text[:150]}')
            return False
        except Exception as e:
            log(f'  ⚠️  WHAPI attempt {attempt + 1} failed: {e}')
            if attempt == 0:
                time.sleep(3)
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

async def main():
    log('=== IVR Extension Report — START ===')

    if not args.nocleanup:
        cleanup_old_files()

    call_data = fetch_greeter_all()
    lms_data = await fetch_lms_all()

    log('Building HTML …')
    html_content = build_html(call_data, lms_data)
    out_path = os.path.join(OUTPUT_DIR, f'IVR_Extension_Report_{RUN_STAMP}.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    log(f'Saved -> {out_path}')

    png_path = out_path.replace('.html', '.png')
    screenshot_ok = await screenshot_html(out_path, png_path)

    if args.local:
        log('--local passed — skipping WhatsApp send')
    elif screenshot_ok:
        ftd_grand = sum_rows([build_row(n, call_data, lms_data, 'ftd') for n in NUMBERS])
        mtd_grand = sum_rows([build_row(n, call_data, lms_data, 'mtd') for n in NUMBERS])
        caption = (
            f'📞 Call Extension Report — {DATE_LABEL}\n'
            f'FTD: {ftd_grand["total_calls"]} calls, {ftd_grand["leads"]} leads, '
            f'{ftd_grand["app"]} app, {ftd_grand["adm"]} adm ({ftd_grand["l2a"]}% L2A)\n'
            f'MTD: {mtd_grand["total_calls"]} calls, {mtd_grand["leads"]} leads, '
            f'{mtd_grand["app"]} app, {mtd_grand["adm"]} adm ({mtd_grand["l2a"]}% L2A)'
        )
        send_via_whapi(png_path, caption, WHATSAPP_GROUP_REGULAR_LMS)
    else:
        log('  ⚠️  Skipping WhatsApp send — screenshot unavailable')

    log('=== Done ===')


if __name__ == '__main__':
    asyncio.run(main())
