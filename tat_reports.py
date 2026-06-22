#!/usr/bin/env python3
"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TAT REPORTS — ICC Flow · Direct Flow · ICC Supervisor Timeline
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Generates 3 standalone HTML reports + screenshots each as full-page PNG:
  1. ICC → Application Timeline  (Supervisor bucket view)
  2. ICC Flow Funnel & TAT       (Supervisor + Counsellor drilldown)
  3. Direct Flow Funnel & TAT    (Supervisor + Counsellor drilldown)

Window  : Last 7 calendar days (IST, before 6 AM = previous day)
DB      : Online LMS (same creds as generate_all_lms_reports.py)
Delivery: PNGs via WHAPI → Online LOB Reports WhatsApp group

Usage:
  python tat_reports.py            # generate + send
  python tat_reports.py --local    # generate only, skip WhatsApp
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio, os, sys, time, html as _html
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

LOCAL_MODE = '--local' in sys.argv

import asyncpg, requests
from datetime import datetime, timedelta, UTC
from dotenv import load_dotenv

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR    = os.environ.get('WORKSPACE_DIR', _SCRIPT_DIR)
load_dotenv(os.path.join(BASE_DIR, '.env'))

OUTPUT_DIR = os.path.join(BASE_DIR, 'Automation Cron Job', 'Target Report')
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Date logic (IST; before 6 AM → use previous day) ────────────────────────
if os.getenv('REPORT_DATE'):
    _ref = datetime.strptime(os.getenv('REPORT_DATE'), '%Y-%m-%d')
else:
    _now_ist = datetime.now(UTC) + timedelta(hours=5, minutes=30)
    _ref     = _now_ist - timedelta(days=1) if _now_ist.hour < 11 else _now_ist

WINDOW_END   = _ref.strftime('%Y-%m-%d')
WINDOW_START = (_ref - timedelta(days=7)).strftime('%Y-%m-%d')
RUN_STAMP    = _ref.strftime('%Y-%m-%d_%H-%M')
WINDOW_LABEL = f'{WINDOW_START} – {WINDOW_END}'

print(f"📅 TAT window: {WINDOW_LABEL}")

# ── DB ───────────────────────────────────────────────────────────────────────
ONLINE_DB = {
    "host":     os.getenv("ONLINE_LMS_DB_HOST"),
    "port":     int(os.getenv("ONLINE_LMS_DB_PORT", "54321")),
    "database": os.getenv("ONLINE_LMS_DB_NAME"),
    "user":     os.getenv("ONLINE_LMS_DB_USER"),
    "password": os.getenv("ONLINE_LMS_DB_PASSWORD"),
}

# ── WhatsApp ─────────────────────────────────────────────────────────────────
WHAPI_TOKEN    = os.getenv('WHAPI_TOKEN_PAID')
_LOB_IDS       = [g.strip() for g in os.getenv('WHATSAPP_GROUP_ONLINE_LOB', '').split(',') if g.strip()]
_ALL_IDS       = [g.strip() for g in os.getenv('WHATSAPP_GROUP_ALL_REPORTS', '').split(',') if g.strip()]
SEND_GROUPS    = list(dict.fromkeys(_LOB_IDS + _ALL_IDS))


# ═══════════════════════════════════════════════════════════════════════════════
# SQL QUERIES
# ═══════════════════════════════════════════════════════════════════════════════

_ICC_SUPERVISOR_SQL = """
WITH cohort AS (
  SELECT s.student_id, s."first_Icc_Date", s.assigned_counsellor_id
  FROM students s
  WHERE s.created_at >= '{ws}'::date - INTERVAL '5 hours 30 minutes'
    AND s.created_at <  '{we}'::date + INTERVAL '1 day' - INTERVAL '5 hours 30 minutes'
    AND s."first_Icc_Date" IS NOT NULL
),
first_app AS (
  SELECT student_id, MIN(created_at) AS first_app_at
  FROM course_status_journeys
  WHERE course_status = 'Application'
    AND student_id IN (SELECT student_id FROM cohort)
  GROUP BY student_id
),
icc_to_app AS (
  SELECT
    c.student_id, c.assigned_counsellor_id, fa.first_app_at,
    CASE
      WHEN fa.first_app_at IS NULL             THEN NULL
      WHEN fa.first_app_at < c."first_Icc_Date" THEN NULL
      ELSE EXTRACT(EPOCH FROM (fa.first_app_at - c."first_Icc_Date")) / 86400
    END AS days_to_app
  FROM cohort c
  LEFT JOIN first_app fa ON c.student_id = fa.student_id
)
SELECT
  mgr.counsellor_name AS to_name,
  COUNT(DISTINCT i.student_id) AS total_icc,
  COUNT(DISTINCT CASE WHEN i.days_to_app IS NOT NULL                      THEN i.student_id END) AS total_applied,
  COUNT(DISTINCT CASE WHEN i.days_to_app < 1                              THEN i.student_id END) AS app_same_day,
  COUNT(DISTINCT CASE WHEN i.days_to_app >= 1 AND i.days_to_app < 2      THEN i.student_id END) AS app_1_to_2,
  COUNT(DISTINCT CASE WHEN i.days_to_app >= 2 AND i.days_to_app < 3      THEN i.student_id END) AS app_2_to_3,
  COUNT(DISTINCT CASE WHEN i.days_to_app >= 3 AND i.days_to_app < 4      THEN i.student_id END) AS app_3_to_4,
  COUNT(DISTINCT CASE WHEN i.days_to_app >= 4 AND i.days_to_app < 5      THEN i.student_id END) AS app_4_to_5,
  COUNT(DISTINCT CASE WHEN i.days_to_app >= 5                             THEN i.student_id END) AS app_5_plus,
  COUNT(DISTINCT CASE WHEN i.days_to_app IS NULL AND i.first_app_at IS NULL THEN i.student_id END) AS not_applied
FROM icc_to_app i
JOIN counsellors l2  ON i.assigned_counsellor_id = l2.counsellor_id AND l2.role = 'l2'
JOIN counsellors mgr ON l2.assigned_to = mgr.counsellor_id          AND mgr.role = 'to'
GROUP BY mgr.counsellor_name
ORDER BY mgr.counsellor_name;
"""

_ICC_FLOW_SQL = """
WITH cohort AS (
  SELECT s.student_id, s."first_Icc_Date", s.created_at AS lead_created_at, s.assigned_counsellor_id
  FROM students s
  WHERE s.created_at >= '{ws}'::date - INTERVAL '5 hours 30 minutes'
    AND s.created_at <  '{we}'::date + INTERVAL '1 day' - INTERVAL '5 hours 30 minutes'
),
first_app AS (
  SELECT student_id, MIN(created_at) AS first_app_at
  FROM course_status_journeys
  WHERE course_status = 'Application' AND student_id IN (SELECT student_id FROM cohort)
  GROUP BY student_id
),
first_adm AS (
  SELECT student_id, MIN(created_at) AS first_adm_at
  FROM course_status_journeys
  WHERE course_status = 'Admission'
    AND INITCAP(TRIM(fee_type)) NOT IN ('Partial Paid','Partially Paid','Partial Done')
    AND student_id IN (SELECT student_id FROM cohort)
  GROUP BY student_id
)
SELECT
  mgr.counsellor_name AS to_name,
  l2.counsellor_name  AS counsellor_name,
  c.student_id,
  DATE(fa.first_app_at   AT TIME ZONE 'Asia/Kolkata') AS app_date,
  DATE(adm.first_adm_at  AT TIME ZONE 'Asia/Kolkata') AS adm_date,
  ROUND(EXTRACT(EPOCH FROM (fa.first_app_at  - c."first_Icc_Date")) / 86400, 2) AS icc_to_app_days,
  ROUND(EXTRACT(EPOCH FROM (adm.first_adm_at - fa.first_app_at))    / 86400, 2) AS app_to_adm_days
FROM cohort c
JOIN counsellors l2  ON c.assigned_counsellor_id = l2.counsellor_id AND l2.role = 'l2'
JOIN counsellors mgr ON l2.assigned_to = mgr.counsellor_id          AND mgr.role = 'to'
JOIN first_app fa    ON c.student_id = fa.student_id
  AND c."first_Icc_Date" IS NOT NULL
  AND fa.first_app_at >= c."first_Icc_Date"
LEFT JOIN first_adm adm ON c.student_id = adm.student_id
ORDER BY mgr.counsellor_name, l2.counsellor_name;
"""

_DIRECT_FLOW_SQL = """
WITH cohort AS (
  SELECT s.student_id, s."first_Icc_Date", s.created_at AS lead_created_at, s.assigned_counsellor_id
  FROM students s
  WHERE s.created_at >= '{ws}'::date - INTERVAL '5 hours 30 minutes'
    AND s.created_at <  '{we}'::date + INTERVAL '1 day' - INTERVAL '5 hours 30 minutes'
),
first_app AS (
  SELECT student_id, MIN(created_at) AS first_app_at
  FROM course_status_journeys
  WHERE course_status = 'Application' AND student_id IN (SELECT student_id FROM cohort)
  GROUP BY student_id
),
first_adm AS (
  SELECT student_id, MIN(created_at) AS first_adm_at
  FROM course_status_journeys
  WHERE course_status = 'Admission'
    AND INITCAP(TRIM(fee_type)) NOT IN ('Partial Paid','Partially Paid','Partial Done')
    AND student_id IN (SELECT student_id FROM cohort)
  GROUP BY student_id
)
SELECT
  mgr.counsellor_name AS to_name,
  l2.counsellor_name  AS counsellor_name,
  c.student_id,
  DATE(fa.first_app_at   AT TIME ZONE 'Asia/Kolkata') AS app_date,
  DATE(adm.first_adm_at  AT TIME ZONE 'Asia/Kolkata') AS adm_date,
  ROUND(EXTRACT(EPOCH FROM (fa.first_app_at  - c.lead_created_at)) / 86400, 2) AS lead_to_app_days,
  ROUND(EXTRACT(EPOCH FROM (adm.first_adm_at - fa.first_app_at))   / 86400, 2) AS app_to_adm_days,
  ROUND(EXTRACT(EPOCH FROM (adm.first_adm_at - c.lead_created_at)) / 86400, 2) AS total_days
FROM cohort c
JOIN counsellors l2  ON c.assigned_counsellor_id = l2.counsellor_id AND l2.role = 'l2'
JOIN counsellors mgr ON l2.assigned_to = mgr.counsellor_id          AND mgr.role = 'to'
JOIN first_app fa    ON c.student_id = fa.student_id
LEFT JOIN first_adm adm ON c.student_id = adm.student_id
WHERE c."first_Icc_Date" IS NULL OR c."first_Icc_Date" > fa.first_app_at
ORDER BY mgr.counsellor_name, l2.counsellor_name;
"""

_LEAD_TO_ICC_SQL = """
WITH cohort AS (
  SELECT
    s.student_id,
    s.created_at AS lead_created_at,
    s."first_Icc_Date",
    s.assigned_counsellor_id
  FROM students s
  WHERE s.created_at >= '{ws}'::date - INTERVAL '5 hours 30 minutes'
    AND s.created_at <  '{we}'::date + INTERVAL '1 day' - INTERVAL '5 hours 30 minutes'
),
lead_to_icc AS (
  SELECT
    c.student_id,
    c.assigned_counsellor_id,
    CASE
      WHEN c."first_Icc_Date" IS NULL THEN NULL
      ELSE EXTRACT(EPOCH FROM (c."first_Icc_Date" - c.lead_created_at)) / 86400
    END AS days_to_icc
  FROM cohort c
)
SELECT
  mgr.counsellor_name AS to_name,
  COUNT(DISTINCT i.student_id)                                                              AS total_leads,
  COUNT(DISTINCT CASE WHEN i.days_to_icc IS NOT NULL THEN i.student_id END)                AS total_icc,
  COUNT(DISTINCT CASE WHEN i.days_to_icc < 1 THEN i.student_id END)                         AS icc_same_day,
  COUNT(DISTINCT CASE WHEN i.days_to_icc >= 1 AND i.days_to_icc < 2 THEN i.student_id END) AS icc_1_to_2,
  COUNT(DISTINCT CASE WHEN i.days_to_icc >= 2 AND i.days_to_icc < 3 THEN i.student_id END) AS icc_2_to_3,
  COUNT(DISTINCT CASE WHEN i.days_to_icc >= 3 AND i.days_to_icc < 4 THEN i.student_id END) AS icc_3_to_4,
  COUNT(DISTINCT CASE WHEN i.days_to_icc >= 4 AND i.days_to_icc < 5 THEN i.student_id END) AS icc_4_to_5,
  COUNT(DISTINCT CASE WHEN i.days_to_icc >= 5 THEN i.student_id END)                       AS icc_5_plus,
  COUNT(DISTINCT CASE WHEN i.days_to_icc IS NULL THEN i.student_id END)                    AS no_icc_yet
FROM lead_to_icc i
JOIN counsellors l2  ON i.assigned_counsellor_id = l2.counsellor_id AND l2.role = 'l2'
JOIN counsellors mgr ON l2.assigned_to = mgr.counsellor_id          AND mgr.role = 'to'
GROUP BY mgr.counsellor_name
ORDER BY mgr.counsellor_name;
"""


# ═══════════════════════════════════════════════════════════════════════════════
# DATA FETCH
# ═══════════════════════════════════════════════════════════════════════════════

async def fetch_all():
    conn = await asyncpg.connect(**ONLINE_DB)
    fmt  = {'ws': WINDOW_START, 'we': WINDOW_END}
    r_l2i  = await conn.fetch(_LEAD_TO_ICC_SQL.format(**fmt))
    r_sup  = await conn.fetch(_ICC_SUPERVISOR_SQL.format(**fmt))
    r_icc  = await conn.fetch(_ICC_FLOW_SQL.format(**fmt))
    r_dir  = await conn.fetch(_DIRECT_FLOW_SQL.format(**fmt))
    await conn.close()
    return ([dict(r) for r in r_l2i], [dict(r) for r in r_sup],
            [dict(r) for r in r_icc], [dict(r) for r in r_dir])


# ═══════════════════════════════════════════════════════════════════════════════
# AGGREGATION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _agg_icc_flow(rows):
    from collections import defaultdict
    B = defaultdict(lambda: {'icc': 0, 'app': 0, 'adm': 0, 'i2f_sum': 0.0, 'f2a_sum': 0.0})
    for r in rows:
        k = (r['to_name'], r['counsellor_name'])
        B[k]['icc'] += 1
        if r['app_date'] is not None:
            B[k]['app'] += 1
            B[k]['i2f_sum'] += float(r['icc_to_app_days'] or 0)
        if r['adm_date'] is not None:
            B[k]['adm'] += 1
            B[k]['f2a_sum'] += float(r['app_to_adm_days'] or 0)
    out = {}
    for k, b in B.items():
        out[k] = {
            'icc':      b['icc'],
            'app':      b['app'],
            'adm':      b['adm'],
            'i2f_pct':  round(b['app'] / b['icc'] * 100, 1) if b['icc'] else None,
            'i2f_tat':  round(b['i2f_sum'] / b['app'], 1)   if b['app'] else None,
            'f2a_pct':  round(b['adm'] / b['app'] * 100, 1) if b['app'] else None,
            'f2a_tat':  round(b['f2a_sum'] / b['adm'], 1)   if b['adm'] else None,
        }
    return out


def _agg_direct_flow(rows):
    from collections import defaultdict
    B = defaultdict(lambda: {'apps': 0, 'adm': 0, 'l2a_sum': 0.0, 'a2d_sum': 0.0})
    for r in rows:
        k = (r['to_name'], r['counsellor_name'])
        B[k]['apps'] += 1
        B[k]['l2a_sum'] += float(r['lead_to_app_days'] or 0)
        if r['adm_date'] is not None:
            B[k]['adm'] += 1
            B[k]['a2d_sum'] += float(r['app_to_adm_days'] or 0)
    out = {}
    for k, b in B.items():
        out[k] = {
            'apps':        b['apps'],
            'adm':         b['adm'],
            'closure_pct': round(b['adm'] / b['apps'] * 100, 1) if b['apps'] else None,
            'lead_to_app': round(b['l2a_sum'] / b['apps'], 1)   if b['apps'] else None,
            'app_to_adm':  round(b['a2d_sum'] / b['adm'],  1)   if b['adm']  else None,
        }
    return out


def _to_order(sup, icc, drct):
    seen = {}
    for r in sup:  seen[r['to_name']] = True
    for r in icc:  seen[r['to_name']] = True
    for r in drct: seen[r['to_name']] = True
    return sorted(seen)


# ═══════════════════════════════════════════════════════════════════════════════
# SHARED CSS  (used by all 3 standalone HTMLs)
# ═══════════════════════════════════════════════════════════════════════════════

_CSS = '''
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --navy:#0d1b2a;--navy2:#1b2e45;
  --teal:#0097a7;
  --white:#fff;--bg:#f4f6f9;--bg2:#eef2f7;
  --bdr:#dce3ed;--bdr2:#b8c4d4;
  --ink:#0d1b2a;--ink2:#4a5568;--ink3:#8896a8;
  --green:#1a7f4b;--green-bg:#e3f5ec;
  --amber:#b45309;--amber-bg:#fef3c7;
  --red:#b91c1c;--red-bg:#fee2e2;
  --blue:#1d4ed8;--blue-bg:#dbeafe;
  --purple:#5b21b6;--purple-bg:#ede9fe;
  --gray:#6b7280;--gray-bg:#f3f4f6;
  --r:6px
}
body{background:var(--bg);font-family:'Segoe UI',-apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif;color:var(--ink);font-size:15px;font-weight:500}
.page{max-width:1180px;margin:0 auto;padding:32px 28px 64px}
/* header */
.hdr{background:var(--navy);padding:26px 28px 22px;border-radius:8px;margin-bottom:22px;border-left:5px solid var(--teal)}
.hdr-row{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}
.brand{font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:var(--teal);margin-bottom:6px;font-weight:700}
.htitle{font-size:clamp(18px,3vw,26px);font-weight:800;color:#fff;line-height:1.2}
.badge-live{display:inline-block;background:var(--teal);color:#fff;font-size:10px;letter-spacing:.1em;text-transform:uppercase;padding:4px 12px;border-radius:20px;margin-bottom:5px;font-weight:700}
.date-meta{font-size:13px;color:rgba(255,255,255,.55);text-align:right;font-weight:500}
/* section label */
.slabel{font-size:13px;font-weight:800;letter-spacing:.07em;text-transform:uppercase;color:var(--navy2);margin:20px 0 14px;padding:0 0 8px;border-bottom:2px solid var(--teal)}
/* kpi cards */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin-bottom:22px}
.card{background:var(--white);border:1px solid var(--bdr);border-radius:var(--r);padding:18px 20px;border-top:4px solid var(--teal)}
.card:nth-child(2){border-top-color:#1a7f4b}.card:nth-child(3){border-top-color:#b45309}
.card:nth-child(4){border-top-color:#b91c1c}.card:nth-child(5){border-top-color:#5b21b6}
.card-lbl{font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--ink3);margin-bottom:5px}
.card-val{font-size:28px;font-weight:800;color:var(--ink);letter-spacing:-.02em;line-height:1.15}
.card-sub{font-size:13px;color:var(--ink3);margin-top:4px;font-weight:500}
/* alert box */
.alert{background:#fffbeb;border:1px solid #fde68a;border-radius:var(--r);padding:12px 16px;margin-bottom:18px;font-size:14px;color:#92400e;line-height:1.6;font-weight:500}
/* note bar */
.note{margin-top:14px;font-size:13px;color:var(--ink3);padding:9px 14px;background:var(--bg2);border-radius:var(--r);border-left:3px solid var(--teal);font-weight:500;line-height:1.6}
/* table */
.tbl-wrap{overflow-x:auto;border-radius:var(--r);border:1px solid var(--bdr2);background:var(--white)}
table{width:100%;border-collapse:collapse;font-size:14px}
thead th{background:var(--navy);color:#fff;padding:13px 14px;text-align:center;font-weight:700;font-size:12px;letter-spacing:.05em;border-right:1px solid rgba(255,255,255,.08)}
thead th:last-child{border-right:none}
thead th.left{text-align:left;padding-left:16px}
thead th.bl{border-left:2px solid rgba(255,255,255,.2)}
thead th.sbl{border-left:1px solid rgba(255,255,255,.1)}
td{padding:11px 14px;border-bottom:1px solid var(--bdr);text-align:right;color:var(--ink2);vertical-align:middle;white-space:nowrap;font-weight:500}
td.left{text-align:left;font-weight:600;color:var(--ink);padding-left:16px}
td.indent{padding-left:32px;color:var(--ink2)!important;font-weight:500!important}
td.num{font-variant-numeric:tabular-nums;font-weight:700;color:var(--ink);font-size:15px}
td.bl{border-left:2px solid var(--bdr)}
td.sbl{border-left:1px solid var(--bdr)}
tbody tr:nth-child(even) td{background:#fafcff}
tbody tr:nth-child(odd)  td{background:var(--white)}
tbody tr:hover td{background:#eef4ff}
tr.to-row   td{background:#eef2f7!important;font-weight:800;color:var(--navy);border-top:2px solid var(--bdr2);font-size:14px}
tr.to-total td{background:var(--bg2)!important;font-weight:700;border-top:1px solid var(--bdr2);color:var(--ink2);font-size:13.5px}
tr.total-row td{background:var(--navy2)!important;color:#fff!important;font-weight:800!important;font-size:15px!important;border-top:2px solid var(--navy)!important}
tr:last-child td{border-bottom:none}
/* badges */
.badge{display:inline-block;padding:3px 11px;border-radius:99px;font-size:13px;font-weight:700}
.bg {background:var(--green-bg);color:var(--green)}
.bb {background:var(--blue-bg);color:var(--blue)}
.ba {background:var(--amber-bg);color:var(--amber)}
.br {background:var(--red-bg);color:var(--red)}
.bp {background:var(--purple-bg);color:var(--purple)}
.bng{background:var(--gray-bg);color:var(--gray)}
.bz {color:#adb5bd;font-size:13px;font-weight:600}
/* TAT colouring */
.tf{color:var(--green);font-weight:800;font-size:14px}
.tm{color:var(--amber);font-weight:800;font-size:14px}
.ts{color:var(--red);font-weight:800;font-size:14px}
/* footer */
.footer{margin-top:32px;padding:14px 0 4px;border-top:1px solid var(--bdr);font-size:12px;color:var(--ink3);text-align:center;font-weight:500}
@media(max-width:640px){.cards{grid-template-columns:1fr 1fr}table{font-size:13px}}
'''


def _html_shell(title, subtitle, window, body_html):
    """Wrap body HTML in a full standalone HTML page."""
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{_html.escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="page">
  <header class="hdr">
    <div class="hdr-row">
      <div>
        <div class="brand">Performance Intelligence · Online LMS</div>
        <h1 class="htitle">{_html.escape(title)}</h1>
        <div style="font-size:12px;color:rgba(255,255,255,.6);margin-top:4px">{_html.escape(subtitle)}</div>
      </div>
      <div style="text-align:right">
        <div class="badge-live">Live Report</div>
        <div class="date-meta">Last 7 days · {_html.escape(window)}</div>
      </div>
    </div>
  </header>
  {body_html}
  <footer class="footer">DegreeFYD · online_lms · {_html.escape(window)} · All times IST</footer>
</div>
</body>
</html>'''


# ═══════════════════════════════════════════════════════════════════════════════
# CELL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def e(v): return _html.escape(str(v))

def _n(v):
    if v is None or v == 0: return '<span class="bz">—</span>'
    return str(int(v))

def _pct_of_applied(count, applied):
    """Bucket cell — % denominator is Total Applied (not ICC)."""
    if count == 0 or applied == 0:
        return '<span class="bz">—</span>'
    pct = count / applied * 100
    cls = 'bg' if pct >= 40 else ('bb' if pct >= 20 else 'ba')
    return f'<span class="badge {cls}">{count}<span style="opacity:.65;font-weight:600"> ({pct:.1f}%)</span></span>'

def _na_cell(count, total):
    """Not-applied badge — high % is bad (red/amber)."""
    if count == 0: return '<span class="bz">—</span>'
    pct = count / total * 100 if total else 0
    cls = 'br' if pct >= 90 else ('ba' if pct >= 70 else 'bng')
    return f'<span class="badge {cls}">{count}<span style="opacity:.65;font-weight:500"> ({pct:.1f}%)</span></span>'

def _applied_total_cell(applied, total):
    """Total-applied badge — % of ICC."""
    if applied == 0: return '<span class="bz">0 (0%)</span>'
    pct = applied / total * 100 if total else 0
    cls = 'bg' if pct >= 30 else ('bb' if pct >= 10 else 'ba')
    return f'<span class="badge {cls}">{applied}<span style="opacity:.65;font-weight:500"> ({pct:.1f}%)</span></span>'

def _pct_badge(p):
    if p is None: return '<span class="bz">—</span>'
    v = float(p)
    cls = 'bg' if v >= 50 else ('bb' if v >= 20 else ('ba' if v > 0 else 'br'))
    return f'<span class="badge {cls}">{v:.1f}%</span>'

def _closure_badge(p):
    if p is None: return '<span class="bz">—</span>'
    v = float(p)
    cls = 'bp' if v >= 75 else ('bb' if v >= 50 else ('ba' if v > 0 else 'br'))
    return f'<span class="badge {cls}">{v:.1f}%</span>'

def _tat(v, fast=1, slow=3):
    if v is None: return '<span class="bz">—</span>'
    d = float(v)
    cls = 'tf' if d <= fast else ('tm' if d <= slow else 'ts')
    return f'<span class="{cls}">{d:.1f}d</span>'

def _total_tat(a, b):
    if a is None or b is None: return '<span class="bz">—</span>'
    t = float(a) + float(b)
    cls = 'tf' if t <= 2 else ('tm' if t <= 5 else 'ts')
    return f'<span class="{cls}">{t:.1f}d</span>'

def _kpis(items):
    inner = ''.join(
        f'<div class="card"><div class="card-lbl">{lbl}</div>'
        f'<div class="card-val">{val}</div>'
        f'<div class="card-sub">{sub}</div></div>'
        for lbl, val, sub in items
    )
    return f'<div class="cards">{inner}</div>'


# ═══════════════════════════════════════════════════════════════════════════════
# HTML REPORT 1 — Lead Gen → ICC Timeline (Supervisor bucket view)
# ═══════════════════════════════════════════════════════════════════════════════

def gen_html_lead_to_icc(l2i_rows):
    tot = {'leads': 0, 'icc': 0, 'dsd': 0, 'd12': 0, 'd23': 0, 'd34': 0, 'd45': 0, 'd5': 0, 'no_icc': 0}
    tbody = ''

    for r in l2i_rows:
        leads  = int(r['total_leads'])
        icc    = int(r['total_icc'])
        dsd    = int(r['icc_same_day'])
        d12    = int(r['icc_1_to_2'])
        d23    = int(r['icc_2_to_3'])
        d34    = int(r['icc_3_to_4'])
        d45    = int(r['icc_4_to_5'])
        d5     = int(r['icc_5_plus'])
        no_icc = int(r['no_icc_yet'])

        for k, v in [('leads', leads), ('icc', icc), ('dsd', dsd), ('d12', d12), ('d23', d23),
                     ('d34', d34), ('d45', d45), ('d5', d5), ('no_icc', no_icc)]:
            tot[k] += v

        tbody += (
            f'<tr>'
            f'<td class="left">{e(r["to_name"])}</td>'
            f'<td class="bl num">{leads}</td>'
            f'<td class="bl">{_applied_total_cell(icc, leads)}</td>'
            f'<td class="bl">{_pct_of_applied(dsd,  icc)}</td>'
            f'<td class="sbl">{_pct_of_applied(d12,  icc)}</td>'
            f'<td class="sbl">{_pct_of_applied(d23,  icc)}</td>'
            f'<td class="sbl">{_pct_of_applied(d34,  icc)}</td>'
            f'<td class="sbl">{_pct_of_applied(d45,  icc)}</td>'
            f'<td class="sbl">{_pct_of_applied(d5,   icc)}</td>'
            f'<td class="bl">{_na_cell(no_icc, leads)}</td>'
            f'</tr>\n'
        )

    tbody += (
        f'<tr class="total-row">'
        f'<td class="left">Grand Total</td>'
        f'<td class="bl num">{tot["leads"]}</td>'
        f'<td class="bl">{_applied_total_cell(tot["icc"], tot["leads"])}</td>'
        f'<td class="bl">{_pct_of_applied(tot["dsd"], tot["icc"])}</td>'
        f'<td class="sbl">{_pct_of_applied(tot["d12"], tot["icc"])}</td>'
        f'<td class="sbl">{_pct_of_applied(tot["d23"], tot["icc"])}</td>'
        f'<td class="sbl">{_pct_of_applied(tot["d34"], tot["icc"])}</td>'
        f'<td class="sbl">{_pct_of_applied(tot["d45"], tot["icc"])}</td>'
        f'<td class="sbl">{_pct_of_applied(tot["d5"],  tot["icc"])}</td>'
        f'<td class="bl">{_na_cell(tot["no_icc"], tot["leads"])}</td>'
        f'</tr>\n'
    )

    icc_pct    = round(tot['icc']    / tot['leads'] * 100, 1) if tot['leads'] else 0
    no_icc_pct = round(tot['no_icc'] / tot['leads'] * 100, 1) if tot['leads'] else 0
    sd_pct     = round(tot['dsd']    / tot['icc']   * 100, 1) if tot['icc']   else 0

    body = (
        _kpis([
            ('Total Leads',       tot['leads'],  'new leads · last 7 days'),
            ('ICC\'d Leads',      tot['icc'],    f'{icc_pct}% of leads ICC\'d'),
            ('ICC\'d Same Day',   tot['dsd'],    f'{sd_pct}% of ICC\'d leads'),
            ('No ICC Yet',        tot['no_icc'], f'{no_icc_pct}% still pending'),
        ]) +
        '<div class="slabel">Supervisor — Lead Generation to ICC Breakdown</div>'
        '<div class="note" style="margin-bottom:14px">💡 <strong>ICC\'d Leads %</strong> = ICC\'d ÷ Total Leads &nbsp;·&nbsp; <strong>Bucket %</strong> = count ÷ Total ICC\'d &nbsp;·&nbsp; <strong>No ICC Yet %</strong> = No ICC ÷ Total Leads &nbsp;·&nbsp; Buckets are mutually exclusive</div>'
        '<div class="tbl-wrap"><table>'
        '<thead>'
        '<tr>'
        '<th class="left" rowspan="2">Supervisor</th>'
        '<th class="bl" rowspan="2">Total Leads</th>'
        '<th class="bl" rowspan="2">ICC\'d Leads</th>'
        '<th class="bl" colspan="6">ICC\'d — days after lead creation</th>'
        '<th class="bl" rowspan="2">No ICC Yet</th>'
        '</tr>'
        '<tr>'
        '<th class="bl">Same Day</th>'
        '<th class="sbl">1–2 days</th>'
        '<th class="sbl">2–3 days</th>'
        '<th class="sbl">3–4 days</th>'
        '<th class="sbl">4–5 days</th>'
        '<th class="sbl">5+ days</th>'
        '</tr>'
        '</thead>'
        f'<tbody>{tbody}</tbody>'
        '</table></div>'
        '<div class="note">Base = all leads with creation date in last 7 days &nbsp;·&nbsp; Bucket % denominator = Total ICC\'d leads &nbsp;·&nbsp; All times IST</div>'
    )

    return _html_shell(
        'Lead Gen → ICC Timeline',
        'Supervisor Bucket View — how fast leads receive their first ICC',
        WINDOW_LABEL, body
    )


# ═══════════════════════════════════════════════════════════════════════════════
# HTML REPORT 2 — ICC → App Timeline (Supervisor bucket view)
# ═══════════════════════════════════════════════════════════════════════════════

def gen_html_supervisor(sup_rows):
    tot = {'icc': 0, 'applied': 0, 'dsd': 0, 'd12': 0, 'd23': 0, 'd34': 0, 'd45': 0, 'd5': 0, 'na': 0}
    tbody = ''

    for r in sup_rows:
        icc     = int(r['total_icc'])
        applied = int(r['total_applied'])
        dsd     = int(r['app_same_day'])
        d12     = int(r['app_1_to_2'])
        d23     = int(r['app_2_to_3'])
        d34     = int(r['app_3_to_4'])
        d45     = int(r['app_4_to_5'])
        d5      = int(r['app_5_plus'])
        na      = int(r['not_applied'])

        for k, v in [('icc', icc), ('applied', applied), ('dsd', dsd), ('d12', d12), ('d23', d23),
                     ('d34', d34), ('d45', d45), ('d5', d5), ('na', na)]:
            tot[k] += v

        tbody += (
            f'<tr>'
            f'<td class="left">{e(r["to_name"])}</td>'
            f'<td class="bl num">{icc}</td>'
            f'<td class="bl">{_applied_total_cell(applied, icc)}</td>'
            f'<td class="bl">{_pct_of_applied(dsd, applied)}</td>'
            f'<td class="sbl">{_pct_of_applied(d12, applied)}</td>'
            f'<td class="sbl">{_pct_of_applied(d23, applied)}</td>'
            f'<td class="sbl">{_pct_of_applied(d34, applied)}</td>'
            f'<td class="sbl">{_pct_of_applied(d45, applied)}</td>'
            f'<td class="sbl">{_pct_of_applied(d5,  applied)}</td>'
            f'<td class="bl">{_na_cell(na, icc)}</td>'
            f'</tr>\n'
        )

    tbody += (
        f'<tr class="total-row">'
        f'<td class="left">Grand Total</td>'
        f'<td class="bl num">{tot["icc"]}</td>'
        f'<td class="bl">{_applied_total_cell(tot["applied"], tot["icc"])}</td>'
        f'<td class="bl">{_pct_of_applied(tot["dsd"], tot["applied"])}</td>'
        f'<td class="sbl">{_pct_of_applied(tot["d12"], tot["applied"])}</td>'
        f'<td class="sbl">{_pct_of_applied(tot["d23"], tot["applied"])}</td>'
        f'<td class="sbl">{_pct_of_applied(tot["d34"], tot["applied"])}</td>'
        f'<td class="sbl">{_pct_of_applied(tot["d45"], tot["applied"])}</td>'
        f'<td class="sbl">{_pct_of_applied(tot["d5"],  tot["applied"])}</td>'
        f'<td class="bl">{_na_cell(tot["na"], tot["icc"])}</td>'
        f'</tr>\n'
    )

    app_pct  = round(tot['applied'] / tot['icc'] * 100, 1) if tot['icc'] else 0
    na_pct   = round(tot['na']      / tot['icc'] * 100, 1) if tot['icc'] else 0
    sd_pct   = round(tot['dsd'] / tot['applied'] * 100, 1) if tot['applied'] else 0

    body = (
        _kpis([
            ('Total ICC',          tot['icc'],     'ICC\'d leads · last 7 days'),
            ('Total Applied',      tot['applied'], f'{app_pct}% of ICC leads applied'),
            ('Applied Same Day',   tot['dsd'],     f'{sd_pct}% of applied'),
            ('Not Applied Yet',    tot['na'],       f'{na_pct}% still pending'),
        ]) +
        '<div class="slabel">Supervisor — ICC to Application Breakdown</div>'
        '<div class="note" style="margin-bottom:14px">💡 <strong>Total Applied %</strong> = Applied ÷ Total ICC &nbsp;·&nbsp; <strong>Bucket %</strong> (same day / 1–2d / etc.) = count ÷ Total Applied &nbsp;·&nbsp; <strong>Not Applied Yet %</strong> = Not Applied ÷ Total ICC &nbsp;·&nbsp; Buckets are mutually exclusive</div>'
        '<div class="tbl-wrap"><table>'
        '<thead>'
        '<tr>'
        '<th class="left" rowspan="2">Supervisor</th>'
        '<th class="bl" rowspan="2">Total ICC</th>'
        '<th class="bl" rowspan="2">Total Applied</th>'
        '<th class="bl" colspan="6">Applied — days after ICC date</th>'
        '<th class="bl" rowspan="2">Not Applied Yet</th>'
        '</tr>'
        '<tr>'
        '<th class="bl">Same Day</th>'
        '<th class="sbl">1–2 days</th>'
        '<th class="sbl">2–3 days</th>'
        '<th class="sbl">3–4 days</th>'
        '<th class="sbl">4–5 days</th>'
        '<th class="sbl">5+ days</th>'
        '</tr>'
        '</thead>'
        f'<tbody>{tbody}</tbody>'
        '</table></div>'
        '<div class="note">Base = ICC\'d leads with lead creation date in last 7 days &nbsp;·&nbsp; All times IST</div>'
    )

    return _html_shell(
        'ICC → Application Timeline',
        'Supervisor Bucket View — how fast leads apply after ICC',
        WINDOW_LABEL, body
    )


# ═══════════════════════════════════════════════════════════════════════════════
# HTML REPORT 2 — ICC Flow Funnel & TAT
# ═══════════════════════════════════════════════════════════════════════════════

def gen_html_icc_flow(icc_rows, to_order):
    agg = _agg_icc_flow(icc_rows)
    grand = {'icc': 0, 'app': 0, 'adm': 0, 'i2f_wsum': 0.0, 'f2a_wsum': 0.0}
    tbody = ''

    for to in to_order:
        team = {k: v for k, v in agg.items() if k[0] == to}
        if not team:
            tbody += (
                f'<tr class="to-row"><td class="left">{e(to)}</td>'
                f'<td class="bl" colspan="7" style="text-align:center;color:#8896a8;font-weight:400">No ICC leads this period</td></tr>\n'
            )
            continue

        t_icc     = sum(v['icc'] for v in team.values())
        t_app     = sum(v['app'] for v in team.values())
        t_adm     = sum(v['adm'] for v in team.values())
        t_i2f_ws  = sum((v['i2f_tat'] or 0) * v['app'] for v in team.values())
        t_f2a_ws  = sum((v['f2a_tat'] or 0) * v['adm'] for v in team.values())
        t_i2f_pct = round(t_app / t_icc * 100, 1) if t_icc else None
        t_i2f_tat = round(t_i2f_ws / t_app, 1)    if t_app  else None
        t_f2a_pct = round(t_adm / t_app * 100, 1)  if t_app  else None
        t_f2a_tat = round(t_f2a_ws / t_adm, 1)     if t_adm  else None

        grand['icc'] += t_icc; grand['app'] += t_app; grand['adm'] += t_adm
        grand['i2f_wsum'] += t_i2f_ws; grand['f2a_wsum'] += t_f2a_ws

        tbody += (
            f'<tr class="to-row">'
            f'<td class="left">{e(to)}</td>'
            f'<td class="bl num">{t_icc}</td>'
            f'<td class="bl num">{_n(t_app)}</td><td class="sbl">{_pct_badge(t_i2f_pct)}</td><td class="sbl">{_tat(t_i2f_tat)}</td>'
            f'<td class="bl num">{_n(t_adm)}</td><td class="sbl">{_pct_badge(t_f2a_pct)}</td><td class="sbl">{_tat(t_f2a_tat)}</td>'
            f'</tr>\n'
        )
        for (_, l2), v in sorted(team.items(), key=lambda x: x[0][1]):
            tbody += (
                f'<tr>'
                f'<td class="left indent">{e(l2)}</td>'
                f'<td class="bl num">{v["icc"]}</td>'
                f'<td class="bl num">{_n(v["app"])}</td><td class="sbl">{_pct_badge(v["i2f_pct"])}</td><td class="sbl">{_tat(v["i2f_tat"])}</td>'
                f'<td class="bl num">{_n(v["adm"])}</td><td class="sbl">{_pct_badge(v["f2a_pct"])}</td><td class="sbl">{_tat(v["f2a_tat"])}</td>'
                f'</tr>\n'
            )
        tbody += (
            f'<tr class="to-total">'
            f'<td class="left indent">Total — {e(to)}</td>'
            f'<td class="bl num">{t_icc}</td>'
            f'<td class="bl num">{_n(t_app)}</td><td class="sbl">{_pct_badge(t_i2f_pct)}</td><td class="sbl">{_tat(t_i2f_tat)}</td>'
            f'<td class="bl num">{_n(t_adm)}</td><td class="sbl">{_pct_badge(t_f2a_pct)}</td><td class="sbl">{_tat(t_f2a_tat)}</td>'
            f'</tr>\n'
        )

    g_i2f_pct = round(grand['app'] / grand['icc'] * 100, 1)     if grand['icc'] else None
    g_i2f_tat = round(grand['i2f_wsum'] / grand['app'], 1)       if grand['app'] else None
    g_f2a_pct = round(grand['adm'] / grand['app'] * 100, 1)      if grand['app'] else None
    g_f2a_tat = round(grand['f2a_wsum'] / grand['adm'], 1)       if grand['adm'] else None

    tbody += (
        f'<tr class="total-row">'
        f'<td class="left">Grand Total</td>'
        f'<td class="bl num">{grand["icc"]}</td>'
        f'<td class="bl num">{grand["app"]}</td><td class="sbl">{_pct_badge(g_i2f_pct)}</td><td class="sbl">{_tat(g_i2f_tat)}</td>'
        f'<td class="bl num">{grand["adm"]}</td><td class="sbl">{_pct_badge(g_f2a_pct)}</td><td class="sbl">{_tat(g_f2a_tat)}</td>'
        f'</tr>\n'
    )

    body = (
        _kpis([
            ('Total ICC',        grand['icc'],                              'last 7 days'),
            ('Apps (I2F)',        grand['app'],                             f'{g_i2f_pct or 0}% of ICC'),
            ('Avg ICC→App TAT',  f'{g_i2f_tat}d' if g_i2f_tat else '—',   'weighted avg days'),
            ('Admissions',       grand['adm'],                              f'{g_f2a_pct or 0}% of apps'),
            ('Avg App→Adm TAT',  f'{g_f2a_tat}d' if g_f2a_tat else '—',   'weighted avg days'),
        ]) +
        '<div class="alert">ICC flow only — direct applications (no ICC, or ICC marked after application) are excluded.</div>'
        '<div class="slabel">Supervisor + Counsellor Drilldown</div>'
        '<div class="tbl-wrap"><table>'
        '<thead>'
        '<tr>'
        '<th class="left" rowspan="2">Supervisor / Counsellor</th>'
        '<th class="bl" rowspan="2">ICC</th>'
        '<th class="bl" colspan="3">Stage 2 — ICC → App</th>'
        '<th class="bl" colspan="3">Stage 3 — App → Adm</th>'
        '</tr>'
        '<tr>'
        '<th class="bl">Apps</th><th class="sbl">I2F %</th><th class="sbl">Avg TAT</th>'
        '<th class="bl">Adm</th><th class="sbl">F2A %</th><th class="sbl">Avg TAT</th>'
        '</tr>'
        '</thead>'
        f'<tbody>{tbody}</tbody>'
        '</table></div>'
        '<div class="note">TAT = weighted average &nbsp;·&nbsp; I2F % = Apps ÷ ICC &nbsp;·&nbsp; F2A % = Adm ÷ Apps &nbsp;·&nbsp; Partial payments excluded</div>'
    )

    return _html_shell(
        'ICC Flow — Funnel & TAT Report',
        'ICC → Application → Admission with average turnaround times',
        WINDOW_LABEL, body
    )


# ═══════════════════════════════════════════════════════════════════════════════
# HTML REPORT 3 — Direct Flow Funnel & TAT
# ═══════════════════════════════════════════════════════════════════════════════

def gen_html_direct_flow(direct_rows, to_order):
    agg   = _agg_direct_flow(direct_rows)
    grand = {'apps': 0, 'adm': 0, 'l2a_ws': 0.0, 'a2d_ws': 0.0}
    tbody = ''

    for to in to_order:
        team = {k: v for k, v in agg.items() if k[0] == to}
        if not team:
            tbody += (
                f'<tr class="to-row"><td class="left">{e(to)}</td>'
                f'<td class="bl" colspan="6" style="text-align:center;color:#8896a8;font-weight:400">No direct applications</td></tr>\n'
            )
            continue

        t_apps = sum(v['apps'] for v in team.values())
        t_adm  = sum(v['adm']  for v in team.values())
        t_l2a  = sum((v['lead_to_app'] or 0) * v['apps'] for v in team.values())
        t_a2d  = sum((v['app_to_adm']  or 0) * v['adm']  for v in team.values())
        t_cls  = round(t_adm  / t_apps * 100, 1) if t_apps else None
        t_l2a_avg = round(t_l2a / t_apps, 1)     if t_apps else None
        t_a2d_avg = round(t_a2d / t_adm,  1)     if t_adm  else None

        grand['apps'] += t_apps; grand['adm'] += t_adm
        grand['l2a_ws'] += t_l2a; grand['a2d_ws'] += t_a2d

        tbody += (
            f'<tr class="to-row">'
            f'<td class="left">{e(to)}</td>'
            f'<td class="bl num">{t_apps}</td><td class="sbl num">{_n(t_adm)}</td><td class="sbl">{_closure_badge(t_cls)}</td>'
            f'<td class="bl">{_tat(t_l2a_avg)}</td><td class="sbl">{_tat(t_a2d_avg)}</td><td class="sbl">{_total_tat(t_l2a_avg, t_a2d_avg)}</td>'
            f'</tr>\n'
        )
        for (_, l2), v in sorted(team.items(), key=lambda x: x[0][1]):
            tbody += (
                f'<tr>'
                f'<td class="left indent">{e(l2)}</td>'
                f'<td class="bl num">{v["apps"]}</td><td class="sbl num">{_n(v["adm"])}</td><td class="sbl">{_closure_badge(v["closure_pct"])}</td>'
                f'<td class="bl">{_tat(v["lead_to_app"])}</td><td class="sbl">{_tat(v["app_to_adm"])}</td><td class="sbl">{_total_tat(v["lead_to_app"], v["app_to_adm"])}</td>'
                f'</tr>\n'
            )
        tbody += (
            f'<tr class="to-total">'
            f'<td class="left indent">Total — {e(to)}</td>'
            f'<td class="bl num">{t_apps}</td><td class="sbl num">{_n(t_adm)}</td><td class="sbl">{_closure_badge(t_cls)}</td>'
            f'<td class="bl">{_tat(t_l2a_avg)}</td><td class="sbl">{_tat(t_a2d_avg)}</td><td class="sbl">{_total_tat(t_l2a_avg, t_a2d_avg)}</td>'
            f'</tr>\n'
        )

    g_cls = round(grand['adm']  / grand['apps'] * 100, 1) if grand['apps'] else None
    g_l2a = round(grand['l2a_ws'] / grand['apps'], 1)     if grand['apps'] else None
    g_a2d = round(grand['a2d_ws'] / grand['adm'],  1)     if grand['adm']  else None

    tbody += (
        f'<tr class="total-row">'
        f'<td class="left">Grand Total</td>'
        f'<td class="bl num">{grand["apps"]}</td><td class="sbl num">{grand["adm"]}</td><td class="sbl">{_closure_badge(g_cls)}</td>'
        f'<td class="bl">{_tat(g_l2a)}</td><td class="sbl">{_tat(g_a2d)}</td><td class="sbl">{_total_tat(g_l2a, g_a2d)}</td>'
        f'</tr>\n'
    )

    total_tat_str = f'{round(float(g_l2a or 0)+float(g_a2d or 0),1)}d' if (g_l2a and g_a2d) else '—'
    body = (
        _kpis([
            ('Direct Apps',      grand['apps'],                              'no ICC before application'),
            ('Admissions',       grand['adm'],                               f'{g_cls or 0}% closure rate'),
            ('Avg Lead→App',     f'{g_l2a}d' if g_l2a else '—',             'lead entry to application'),
            ('Avg App→Adm',      f'{g_a2d}d' if g_a2d else '—',             'application to admission'),
            ('Avg Total TAT',    total_tat_str,                              'lead to admission'),
        ]) +
        '<div class="alert">⚠️ Direct flow = application marked with no ICC, or ICC marked <em>after</em> application. These are process anomalies — leads skipped the counselling stage.</div>'
        '<div class="slabel">Supervisor + Counsellor Drilldown</div>'
        '<div class="tbl-wrap"><table>'
        '<thead>'
        '<tr>'
        '<th class="left" rowspan="2">Supervisor / Counsellor</th>'
        '<th class="bl" colspan="3">Stage 1 — Lead → App</th>'
        '<th class="bl" colspan="3">Stage 2 — App → Adm</th>'
        '</tr>'
        '<tr>'
        '<th class="bl">Direct Apps</th><th class="sbl">Adm</th><th class="sbl">Closure %</th>'
        '<th class="bl">Avg Lead→App</th><th class="sbl">Avg App→Adm</th><th class="sbl">Total TAT</th>'
        '</tr>'
        '</thead>'
        f'<tbody>{tbody}</tbody>'
        '</table></div>'
        '<div class="note">TAT measured from lead creation date &nbsp;·&nbsp; Partial payments excluded &nbsp;·&nbsp; TAT colour: green ≤1d · amber ≤3d · red >3d</div>'
    )

    return _html_shell(
        'Direct Flow — Funnel & TAT Report',
        'Lead → Application → Admission (no ICC path)',
        WINDOW_LABEL, body
    )


# ═══════════════════════════════════════════════════════════════════════════════
# GENERATE ALL HTML FILES
# ═══════════════════════════════════════════════════════════════════════════════

def generate_all_html(l2i_rows, sup_rows, icc_rows, direct_rows):
    order = _to_order(sup_rows, icc_rows, direct_rows)

    files = [
        ('TAT_1_Lead_to_ICC',     gen_html_lead_to_icc(l2i_rows)),
        ('TAT_2_ICC_Supervisor',  gen_html_supervisor(sup_rows)),
        ('TAT_3_ICC_Flow',        gen_html_icc_flow(icc_rows, order)),
        ('TAT_4_Direct_Flow',     gen_html_direct_flow(direct_rows, order)),
    ]

    paths = []
    for name, content in files:
        p = os.path.join(OUTPUT_DIR, f'{name}_{RUN_STAMP}.html')
        with open(p, 'w', encoding='utf-8-sig') as f:
            f.write(content)
        print(f"  ✅ HTML saved: {os.path.basename(p)}")
        paths.append(p)

    return paths


# ═══════════════════════════════════════════════════════════════════════════════
# SCREENSHOT (full page, one file per report)
# ═══════════════════════════════════════════════════════════════════════════════

async def screenshot_all(html_paths, png_paths):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("  ⚠️  playwright not installed — run: pip install playwright && playwright install chromium")
        return [False] * len(html_paths)

    results = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=['--no-sandbox', '--disable-setuid-sandbox'])
            for html_path, png_path in zip(html_paths, png_paths):
                try:
                    page = await browser.new_page(
                        viewport={'width': 1400, 'height': 900},
                        device_scale_factor=2,
                    )
                    await page.goto(f'file:///{os.path.abspath(html_path)}', wait_until='networkidle', timeout=30000)
                    # full-page screenshot — captures everything without tab clipping
                    await page.screenshot(path=png_path, full_page=True)
                    await page.close()
                    print(f"  ✅ Screenshot: {os.path.basename(png_path)}")
                    results.append(True)
                except Exception as ex:
                    print(f"  ⚠️  Screenshot failed ({os.path.basename(html_path)}): {ex}")
                    results.append(False)
            await browser.close()
    except Exception as ex:
        print(f"  ⚠️  Browser error: {ex}")
        while len(results) < len(html_paths):
            results.append(False)
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# WHAPI SEND
# ═══════════════════════════════════════════════════════════════════════════════

def send_via_whapi(file_path, caption, group_id):
    if not WHAPI_TOKEN:
        print(f"  ⚠️  No WHAPI token — skipping {os.path.basename(file_path)}")
        return False
    if not group_id:
        print(f"  ⚠️  No group ID — skipping {os.path.basename(file_path)}")
        return False

    import base64
    with open(file_path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode()

    filename = os.path.basename(file_path)
    payload  = {
        'to':      group_id,
        'media':   f'data:image/png;name={filename};base64,{b64}',
        'caption': caption,
    }
    headers = {
        'accept':        'application/json',
        'authorization': f'Bearer {WHAPI_TOKEN}',
        'content-type':  'application/json',
    }
    for attempt in range(2):
        try:
            r = requests.post('https://gate.whapi.cloud/messages/image',
                              headers=headers, json=payload, timeout=20)
            if 200 <= r.status_code < 300:
                print(f"  ✅ Sent to {group_id[:12]}…: {filename}")
                return True
            print(f"  ⚠️  WHAPI HTTP {r.status_code}: {r.text[:120]}")
            return False
        except requests.exceptions.Timeout:
            print(f"  ⚠️  WHAPI timeout (attempt {attempt+1})")
            if attempt == 0: time.sleep(3)
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

async def main():
    print("=" * 60)
    print("📊 TAT REPORTS — Lead→ICC · ICC→App · Direct Flow · Supervisor Timeline")
    print("=" * 60)
    print(f"   Window: {WINDOW_LABEL}")
    print()

    # Step 1 — fetch ──────────────────────────────────────────────────────────
    print("─── Step 1/3: Fetching data from Online LMS DB ─────────────────────")
    l2i_rows, sup_rows, icc_rows, direct_rows = await fetch_all()
    print(f"  ✅ Lead→ICC Supervisor rows: {len(l2i_rows)} supervisors")
    print(f"  ✅ ICC Supervisor rows     : {len(sup_rows)} supervisors")
    print(f"  ✅ ICC Flow students       : {len(icc_rows)}")
    print(f"  ✅ Direct Flow students    : {len(direct_rows)}")
    print()

    # Step 2 — generate HTMLs ─────────────────────────────────────────────────
    print("─── Step 2/3: Generating HTML reports ───────────────────────────────")
    html_paths = generate_all_html(l2i_rows, sup_rows, icc_rows, direct_rows)
    print()

    # Step 3 — screenshot + send ──────────────────────────────────────────────
    print("─── Step 3/3: Screenshot + WhatsApp Delivery ────────────────────────")

    NAMES    = ['Lead_to_ICC_Timeline', 'ICC_Supervisor_Timeline', 'ICC_Flow_TAT', 'Direct_Flow_TAT']
    CAPTIONS = [
        f'Lead Gen → ICC Timeline — Supervisor Bucket View | {WINDOW_LABEL}',
        f'ICC → App Timeline — Supervisor Bucket View | {WINDOW_LABEL}',
        f'ICC Flow — Funnel & TAT (Supervisor + Counsellor) | {WINDOW_LABEL}',
        f'Direct Flow — Funnel & TAT (no ICC path) | {WINDOW_LABEL}',
    ]
    png_paths = [
        os.path.join(OUTPUT_DIR, f'TAT_{name}_{RUN_STAMP}.png')
        for name in NAMES
    ]

    print("  [3a] Taking full-page screenshots...")
    ok_flags  = await screenshot_all(html_paths, png_paths)
    sent_list = [(p, cap) for p, ok, cap in zip(png_paths, ok_flags, CAPTIONS) if ok]
    print()

    if LOCAL_MODE:
        print("  [3b] LOCAL MODE — skipping WhatsApp")
        for p, _ in sent_list:
            print(f"       📁 {os.path.basename(p)}")
    else:
        print(f"  [3b] Sending {len(sent_list)} screenshots via WHAPI → {len(SEND_GROUPS)} group(s)...")
        for png_path, caption in sent_list:
            for gid in SEND_GROUPS:
                send_via_whapi(png_path, caption, gid)

    print()
    print("=" * 60)
    print("✅ TAT REPORTS COMPLETE")
    print(f"   Window    : {WINDOW_LABEL}")
    print(f"   HTMLs     : {len(html_paths)}")
    print(f"   PNGs      : {len(sent_list)}/{len(NAMES)} succeeded")
    print(f"   WhatsApp  : {'skipped (--local)' if LOCAL_MODE else f'{len(SEND_GROUPS)} group(s)'}")
    print("=" * 60)


if __name__ == '__main__':
    asyncio.run(main())
