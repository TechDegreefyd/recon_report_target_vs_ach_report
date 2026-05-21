#!/usr/bin/env python3
"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
UNIFIED REGULAR API RECON REPORTS — ALL Sources + Branded Campaigns
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Single script that generates BOTH recon reports:
  1. ALL Sources  — Every API recon entry for the day
  2. Branded Campaigns — Only students matching branded UTM campaign patterns

Usage:  cd /workspace && python3 "Automation Cron Job/generate_all_recon_reports.py"
Environment:  .env at WORKSPACE_DIR (default /home/mohit/workspace)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import os
import sys

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
import json
import base64
import time
import html as html_mod
from datetime import datetime, timedelta
from dotenv import load_dotenv

try:
    import asyncpg
    import requests
except ImportError as e:
    print(f"❌ Missing dependency: {e}")
    print("   Run: pip install asyncpg requests python-dotenv")
    sys.exit(1)

# ─── PATHS ──────────────────────────────────────────────────────────────────────
# Always resolve relative to this script's location — works on any machine
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))

OUTPUT_DIR = os.path.join(BASE_DIR, 'Automation Cron Job', 'Recon Data')
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ─── WHAPI ──────────────────────────────────────────────────────────────────────
WHAPI_TOKEN = os.getenv('WHAPI_TOKEN')
WHATSAPP_GROUP = os.getenv('WHATSAPP_GROUP', '120363426619711887@g.us')

# ─── DB ─────────────────────────────────────────────────────────────────────────
DB_CONFIG = {
    "host": os.getenv("REGULAR_LMS_DB_HOST"),
    "port": int(os.getenv("REGULAR_LMS_DB_PORT", "54321")),
    "database": os.getenv("REGULAR_LMS_DB_NAME"),
    "user": os.getenv("REGULAR_LMS_DB_USER"),
    "password": os.getenv("REGULAR_LMS_DB_PASSWORD"),
}

# ─── DATE LOGIC (IST, before 6AM = previous day) ────────────────────────────────
if len(sys.argv) > 1:
    REPORT_DATE_STR = sys.argv[1]
else:
    now_utc = datetime.utcnow()
    now_ist = now_utc + timedelta(hours=5, minutes=30)
    report_date = now_ist - timedelta(days=1) if now_ist.hour < 6 else now_ist
    REPORT_DATE_STR = report_date.strftime('%Y-%m-%d')

# Timestamp of this run (IST) — appended to all output filenames
_run_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
RUN_STAMP = _run_ist.strftime('%Y-%m-%d_%H-%M')

print(f"📅 Report Date: {REPORT_DATE_STR}")


# ═══════════════════════════════════════════════════════════════════════════════
# BRANDED CAMPAIGN PATTERNS
# ═══════════════════════════════════════════════════════════════════════════════

BRANDED_PATTERNS = [
    'LPU_Online', 'CU_Online', 'cu_online', 'Amity_Online',
    'Amity_University', 'Partner_Amity', 'Shoolini_Online',
    'Galgotias', 'VGU_Online', 'Manipal_Online', 'GLA_Online',
    'GLA_University', 'IGNOU', 'UA_MBA', 'F_UA'
]

BRANDED_CAMPAIGN_IDS = {
    '23659350616', '23807086200', '23810994645', '23814823859',
    '23820721369', '23821027168', '23228113322', '23794794232',
    '23794010280', '23772025619', '23779002914', '23794940566',
    '23767340817', '23798269338', '23772157658', '23803352159',
    '23470383548', '23502437890', '23676777747', '23534722448',
    '23486436393', '23486463996', '23675435222'
}

STATUS_NORMALIZE = {
    'Proceed': 'Proceed',
    'Failed due to Technical Issues': 'Failed',
    'Do not Proceed': 'Do not Proceed',
    'Do not Proceed (Still) ': 'Do not Proceed',
    'Field Missing': 'Field Missing',
}


# ═══════════════════════════════════════════════════════════════════════════════
# DB QUERIES
# ═══════════════════════════════════════════════════════════════════════════════

async def fetch_all_sources(date_str):
    """Fetch ALL recon rows for the given date."""
    conn = await asyncpg.connect(**DB_CONFIG)
    try:
        rows = await conn.fetch("""
            SELECT
                r.college_name,
                r.sent_type,
                r.api_sent_status,
                COUNT(DISTINCT r.student_id) AS lead_count
            FROM student_college_api_sent_status r
            WHERE DATE(r.created_at AT TIME ZONE 'Asia/Kolkata') = $1::date
            GROUP BY 1, 2, 3
            ORDER BY 1, 2, 3
        """, datetime.strptime(date_str, '%Y-%m-%d').date())
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def fetch_branded_student_ids(date_str):
    """Get student_ids matching branded UTM campaigns."""
    conn = await asyncpg.connect(**DB_CONFIG)
    try:
        # Step 1: Get all students with recon entries for this date
        recon_students = await conn.fetch("""
            SELECT DISTINCT r.student_id
            FROM student_college_api_sent_status r
            WHERE DATE(r.created_at AT TIME ZONE 'Asia/Kolkata') = $1::date
        """, datetime.strptime(date_str, '%Y-%m-%d').date())

        recon_ids = [r['student_id'] for r in recon_students]
        if not recon_ids:
            return set(), {}

        # Step 2: Get latest UTM data per student (DISTINCT ON)
        utm_rows = await conn.fetch("""
            SELECT DISTINCT ON (student_id)
                student_id,
                utm_campaign,
                utm_campaign_id
            FROM student_lead_activities
            WHERE student_id = ANY($1::text[])
            ORDER BY student_id, created_at DESC
        """, recon_ids)

        # Step 3: In-Python matching
        branded_ids = set()
        branded_info = {}

        for row in utm_rows:
            sid = row['student_id']
            campaign = (row['utm_campaign'] or '')
            camp_id = str(row['utm_campaign_id'] or '')

            is_branded = False
            matched = None

            for pattern in BRANDED_PATTERNS:
                if pattern.lower() in campaign.lower():
                    is_branded = True
                    matched = f"campaign:{pattern}"
                    break

            if not is_branded and camp_id in BRANDED_CAMPAIGN_IDS:
                is_branded = True
                matched = f"campaign_id:{camp_id}"

            if is_branded:
                branded_ids.add(sid)
                branded_info[sid] = {'utm_campaign': campaign, 'utm_campaign_id': camp_id, 'matched_by': matched}

        print(f"\n  Branded matching:")
        print(f"    Recon students: {len(recon_ids)}")
        print(f"    With UTM data:  {len(utm_rows)}")
        print(f"    Branded match:  {len(branded_ids)}")

        sample = 0
        for sid, info in list(branded_info.items())[:5]:
            print(f"    {sid}: campaign='{info['utm_campaign']}' match={info['matched_by']}")
            sample += 1

        return branded_ids, branded_info
    finally:
        await conn.close()


async def fetch_branded_recon(date_str, branded_ids):
    """Fetch recon rows filtered to branded students only."""
    if not branded_ids:
        return []

    conn = await asyncpg.connect(**DB_CONFIG)
    try:
        rows = await conn.fetch("""
            SELECT
                r.college_name,
                r.sent_type,
                r.api_sent_status,
                COUNT(DISTINCT r.student_id) AS lead_count
            FROM student_college_api_sent_status r
            WHERE DATE(r.created_at AT TIME ZONE 'Asia/Kolkata') = $1::date
              AND r.student_id = ANY($2::text[])
            GROUP BY 1, 2, 3
            ORDER BY 1, 2, 3
        """, datetime.strptime(date_str, '%Y-%m-%d').date(), list(branded_ids))
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def fetch_lms_lead_counts(date_str, branded_ids=None):
    """Fetch LMS leads per college from student_lead_activities JOINed with recon."""
    conn = await asyncpg.connect(**DB_CONFIG)
    try:
        if branded_ids:
            rows = await conn.fetch("""
                SELECT
                    r.college_name,
                    COUNT(DISTINCT la.student_id) AS lms_leads
                FROM student_lead_activities la
                INNER JOIN student_college_api_sent_status r
                    ON la.student_id = r.student_id
                WHERE DATE(r.created_at AT TIME ZONE 'Asia/Kolkata') = $1::date
                  AND r.student_id = ANY($2::text[])
                GROUP BY r.college_name
                ORDER BY r.college_name
            """, datetime.strptime(date_str, '%Y-%m-%d').date(), list(branded_ids))
        else:
            rows = await conn.fetch("""
                SELECT
                    r.college_name,
                    COUNT(DISTINCT la.student_id) AS lms_leads
                FROM student_lead_activities la
                INNER JOIN student_college_api_sent_status r
                    ON la.student_id = r.student_id
                WHERE DATE(r.created_at AT TIME ZONE 'Asia/Kolkata') = $1::date
                GROUP BY r.college_name
                ORDER BY r.college_name
            """, datetime.strptime(date_str, '%Y-%m-%d').date())
        return [dict(r) for r in rows]
    finally:
        await conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# DATA TRANSFORM
# ═══════════════════════════════════════════════════════════════════════════════

def transform_to_matrix(rows):
    """
    Transform flat rows into college-wise matrix:
    {college: {'auto': {status: count}, 'manual': {status: count}}}
    """
    matrix = {}
    for row in rows:
        college = row['college_name']
        sent_type = row['sent_type']  # 'auto', 'manual', or 'bot'
        status = STATUS_NORMALIZE.get(row['api_sent_status'], row['api_sent_status'])
        count = row['lead_count']

        if college not in matrix:
            matrix[college] = {}

        if sent_type not in matrix[college]:
            matrix[college][sent_type] = {}

        matrix[college][sent_type][status] = matrix[college][sent_type].get(status, 0) + count

    return matrix


# ═══════════════════════════════════════════════════════════════════════════════
# HTML GENERATION (Dark Theme with Barlow font)
# ═══════════════════════════════════════════════════════════════════════════════

def _v(n):
    """Return number or em-dash for zero."""
    return '&mdash;' if not n else str(n)


def generate_html(matrix, report_date, lms_leads, title, filename):
    """Generate HTML report matching the original dark format."""
    lms_lookup = {l['college_name']: l['lms_leads'] for l in lms_leads}
    colleges = sorted(matrix.keys())

    # Format date nicely e.g. "20 May 2026"
    try:
        dt = datetime.strptime(report_date, '%Y-%m-%d')
        date_display = dt.strftime('%-d %B %Y')
    except ValueError:
        try:
            dt = datetime.strptime(report_date, '%Y-%m-%d')
            date_display = dt.strftime('%d %B %Y').lstrip('0')
        except Exception:
            date_display = report_date
    # Windows-safe date formatting (no %-d)
    try:
        date_display = dt.strftime('%#d %B %Y')
    except Exception:
        date_display = dt.strftime('%d %B %Y').lstrip('0') or dt.strftime('%d %B %Y')

    # badge label from title
    badge = 'All Sources' if 'All' in title else 'Branded Campaigns'

    # Accumulate per-college: auto and manual (bot merged into auto)
    # statuses: Proceed, Failed→Fail, Do not Proceed→DNP
    rows_html = ''
    gt_auto = {'p': 0, 'f': 0, 'd': 0}
    gt_manual = {'p': 0, 'f': 0, 'd': 0}

    for college in colleges:
        data = matrix[college]

        def get(sent_type, status):
            return data.get(sent_type, {}).get(status, 0)

        # auto + bot merged into auto
        ap = get('auto', 'Proceed') + get('bot', 'Proceed')
        af = get('auto', 'Failed') + get('bot', 'Failed')
        ad = get('auto', 'Do not Proceed') + get('bot', 'Do not Proceed')

        mp = get('manual', 'Proceed')
        mf = get('manual', 'Failed')
        md = get('manual', 'Do not Proceed')

        tp = ap + mp
        tf = af + mf
        td = ad + md
        lms = lms_lookup.get(college, 0)

        gt_auto['p'] += ap; gt_auto['f'] += af; gt_auto['d'] += ad
        gt_manual['p'] += mp; gt_manual['f'] += mf; gt_manual['d'] += md

        rows_html += f"""<tr>
<td class=td-college>{college}</td>
<td class=bl>{_v(ap)}</td><td class=fail>{_v(af)}</td><td>{_v(ad)}</td>
<td class=bl>{_v(mp)}</td><td class=fail>{_v(mf)}</td><td>{_v(md)}</td>
<td class=t-proc>{_v(tp)}</td><td class=t-fail>{_v(tf)}</td><td class=t-dnp>{_v(td)}</td>
<td class=leads>{_v(lms)}</td></tr>\n"""

    # Grand total row
    gtp = gt_auto['p'] + gt_manual['p']
    gtf = gt_auto['f'] + gt_manual['f']
    gtd = gt_auto['d'] + gt_manual['d']
    gt_lms = sum(lms_lookup.values())
    grand_total = gtp + gtf + gtd

    grand_row = f"""<tr class=grand>
<td class=td-college>Grand Total</td>
<td class=bl>{_v(gt_auto['p'])}</td><td class=fail>{_v(gt_auto['f'])}</td><td>{_v(gt_auto['d'])}</td>
<td class=bl>{_v(gt_manual['p'])}</td><td class=fail>{_v(gt_manual['f'])}</td><td>{_v(gt_manual['d'])}</td>
<td class=t-proc>{_v(gtp)}</td><td class=t-fail>{_v(gtf)}</td><td class=t-dnp>{_v(gtd)}</td>
<td class=leads>{_v(gt_lms)}</td></tr>\n"""

    # Summary row: auto total | manual total | grand total | lms
    auto_sum = gt_auto['p'] + gt_auto['f'] + gt_auto['d']
    manual_sum = gt_manual['p'] + gt_manual['f'] + gt_manual['d']

    summary_row = f"""<tr class=summary>
<td class=sum-label>Summary Total</td>
<td colspan=3 style="border-left:1px solid #1e5a82">{auto_sum}</td>
<td colspan=3 style="border-left:1px solid #1e5a82">{manual_sum}</td>
<td colspan=3 style="border-left:1px solid #1e5a82">{grand_total}</td>
<td class=leads>{gt_lms}</td></tr>\n"""

    css = """@import url('https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;600;700&family=Barlow+Condensed:wght@600;700&display=swap');
*{box-sizing:border-box;margin:0;padding:0}.rr-root{background:#0b1623;border-radius:14px;padding:32px 36px;font-family:'Barlow',sans-serif;color:#e2eaf4}
.rr-top-badge{display:inline-block;background:#0e3d5c;color:#39b8f5;font-size:10px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;padding:4px 12px;border-radius:5px;margin-bottom:10px;border:1px solid #1a5a80}
.rr-header-row{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:28px}
.rr-title{font-family:'Barlow Condensed',sans-serif;font-size:30px;font-weight:700;color:#39b8f5;letter-spacing:.04em;text-transform:uppercase;line-height:1.1}
.rr-date-box{background:#111e2e;border:1px solid #1e3a52;border-radius:8px;padding:8px 18px;font-size:14px;font-weight:600;color:#7faec9;white-space:nowrap;align-self:center}
.rr-table-wrap{border-radius:10px;overflow:hidden;border:1px solid #1a2e42}table{width:100%;border-collapse:collapse}
th{text-align:center;padding:0}.th-college{text-align:left;padding:14px 16px;font-size:10px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:#4d7a99;background:#0d1e2e}
.th-group{background:#0d1e2e;padding:13px 6px;font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:#39b8f5;border-left:1px solid #1a2e42}
.th-group-leads{background:#0d2a1a;padding:13px 6px;font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#3ddc84;border-left:2px solid #1f5c30}
.th-sub{background:#0a1825;font-size:10px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;padding:8px 4px;border-top:1px solid #13253a}.th-sub.proc{color:#4d7a99;border-left:1px solid #1a2e42}.th-sub.fail{color:#e05c5c}.th-sub.dnp{color:#4d7a99}.th-sub.t-proc{color:#39b8f5;font-weight:700;border-left:1px solid #1a2e42}.th-sub.t-fail{color:#e05c5c;font-weight:700}.th-sub.t-dnp{color:#4d7a99;font-weight:700}
td{text-align:center;padding:14px 4px;font-size:15px;font-weight:500;color:#b8cfe0;background:#0b1623;border-top:1px solid #13253a}
td.td-college{text-align:left;padding:14px 16px;font-size:14px;font-weight:700;color:#d4e6f4;background:#0b1623}td.bl{border-left:1px solid #1a2e42}td.fail{color:#e05c5c}td.t-proc{color:#39b8f5;font-weight:700;border-left:1px solid #1a2e42}td.t-fail{color:#e05c5c;font-weight:700}td.t-dnp{color:#b8cfe0;font-weight:700}
td.leads{color:#3ddc84;font-weight:700;font-size:17px;background:#081410;border-left:2px solid #1f5c30}
tr.grand td{background:#0e2235;border-top:2px solid #1c3d5a}tr.grand td.td-college{color:#39b8f5;font-weight:700;background:#0e2235}tr.grand td{color:#39b8f5;font-weight:700}tr.grand td.fail{color:#e05c5c;font-weight:700}tr.grand td.leads{color:#3ddc84;font-weight:700;background:#060e09}
tr.summary td{background:#163a56;border-top:2px solid #1e5a82;padding:15px 4px;font-size:17px;font-weight:700;color:#fff}tr.summary td.sum-label{text-align:left;padding-left:16px;font-size:14px;font-weight:600;color:#c8dff0}tr.summary td.leads{color:#3ddc84;font-size:20px;font-weight:700;background:#081a0e;border-left:2px solid #1f5c30}
.rr-note{margin-top:14px;font-size:11px;color:#2e5570;text-align:right;letter-spacing:.04em}"""

    html_doc = f"""<!DOCTYPE html><html><head><meta charset=UTF-8>
<style>{css}</style></head><body><div class=rr-root>
<div class=rr-header-row><div><div class=rr-top-badge>{badge}</div><div class=rr-title>Regular API Recon Report</div></div><div class=rr-date-box>{date_display}</div></div>
<div class=rr-table-wrap><table>
<thead><tr><th class=th-college rowspan=2>College Name</th><th class=th-group colspan=3>Auto Recon</th><th class=th-group colspan=3>Manual Recon</th><th class=th-group colspan=3>Total</th><th class=th-group-leads rowspan=2>Total<br>Leads</th></tr>
<tr><th class="th-sub proc">Proceed</th><th class="th-sub fail">Fail</th><th class="th-sub dnp">DNP</th><th class="th-sub proc">Proceed</th><th class="th-sub fail">Fail</th><th class="th-sub dnp">DNP</th><th class="th-sub t-proc">Proceed</th><th class="th-sub t-fail">Fail</th><th class="th-sub t-dnp">DNP</th></tr></thead>
<tbody>{rows_html}{grand_row}{summary_row}</tbody></table></div>
<div class=rr-note>{grand_total} total records | {date_display}</div></div></body></html>"""

    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(html_doc)

    stats = {
        'total_proceed': gtp, 'total_fail': gtf, 'total_dnp': gtd,
        'total_all': grand_total, 'total_lms': gt_lms,
        'colleges': len(colleges), 'filepath': filepath
    }

    print(f"  ✅ Saved: {filename} ({grand_total} total, {gt_lms} LMS leads, {len(colleges)} colleges)")
    return filepath, stats


# ═══════════════════════════════════════════════════════════════════════════════
# WHAPI SEND
# ═══════════════════════════════════════════════════════════════════════════════

def send_via_whapi(file_path, caption):
    """Send a file to WhatsApp via WHAPI (best-effort)."""
    if not WHAPI_TOKEN:
        print(f"  ⚠️  WHAPI_TOKEN not set — file kept at: {file_path}")
        return False

    filename = os.path.basename(file_path)
    with open(file_path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode('utf-8')

    media_data = f'data:text/html;name={filename};base64,{b64}'
    payload = {'to': WHATSAPP_GROUP, 'media': media_data, 'caption': caption}
    headers = {'accept': 'application/json', 'authorization': f'Bearer {WHAPI_TOKEN}',
               'content-type': 'application/json'}

    for attempt in range(2):
        try:
            r = requests.post('https://gate.whapi.cloud/messages/document',
                              headers=headers, json=payload, timeout=20)
            if 200 <= r.status_code < 300:
                print(f"  ✅ WHAPI sent: {filename}")
                return True
            print(f"  ⚠️  WHAPI HTTP {r.status_code}: {r.text[:120]}")
            return False
        except requests.exceptions.Timeout:
            print(f"  ⚠️  WHAPI timeout (network unreachable?)")
            return False
        except Exception as e:
            print(f"  ⚠️  WHAPI error: {e}")
            if attempt == 0:
                time.sleep(3)
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

async def main():
    print("=" * 60)
    print("📊 REGULAR API RECON REPORTS — ALL Sources + Branded")
    print("=" * 60)
    print(f"   Date: {REPORT_DATE_STR}")

    # ── STEP 1: ALL Sources ──────────────────────────────────────────────
    print("\n─── ALL Sources ─────────────────────────────────────────────────")
    all_rows = await fetch_all_sources(REPORT_DATE_STR)
    print(f"  Rows: {len(all_rows)}")
    for r in all_rows:
        print(f"    {r['college_name']} | {r['sent_type']} | {r['api_sent_status']} | {r['lead_count']}")

    all_matrix = transform_to_matrix(all_rows)
    print(f"  Colleges: {len(all_matrix)}")

    all_lms = await fetch_lms_lead_counts(REPORT_DATE_STR)
    print(f"  LMS lead records: {len(all_lms)}")
    for l in all_lms:
        print(f"    {l['college_name']}: {l['lms_leads']}")

    all_file, all_stats = generate_html(
        all_matrix, REPORT_DATE_STR, all_lms,
        "Regular API Recon \u2014 All Sources",
        f"Regular_Recon_All_{RUN_STAMP}.html"
    )

    # ── STEP 2: Branded Campaigns ────────────────────────────────────────
    print("\n─── Branded Campaigns ────────────────────────────────────────────")
    branded_ids, branded_info = await fetch_branded_student_ids(REPORT_DATE_STR)

    branded_rows = await fetch_branded_recon(REPORT_DATE_STR, branded_ids)
    print(f"  Rows: {len(branded_rows)}")
    for r in branded_rows:
        print(f"    {r['college_name']} | {r['sent_type']} | {r['api_sent_status']} | {r['lead_count']}")

    branded_matrix = transform_to_matrix(branded_rows)
    print(f"  Colleges: {len(branded_matrix)}")

    branded_lms = await fetch_lms_lead_counts(REPORT_DATE_STR, branded_ids)
    print(f"  Branded LMS lead records: {len(branded_lms)}")

    branded_file, branded_stats = generate_html(
        branded_matrix, REPORT_DATE_STR, branded_lms,
        "Regular API Recon \u2014 Branded Campaigns",
        f"Regular_Recon_Branded_{RUN_STAMP}.html"
    )

    # ── STEP 3: Summary ──────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("SUMMARY STATS")
    print("=" * 60)
    print(f"\n  ALL SOURCES — {REPORT_DATE_STR}")
    for k, v in all_stats.items():
        if k != 'filepath':
            print(f"    {k}: {v}")

    print(f"\n  BRANDED — {REPORT_DATE_STR}")
    for k, v in branded_stats.items():
        if k != 'filepath':
            print(f"    {k}: {v}")

    # ── STEP 4: WHAPI send (best-effort) ─────────────────────────────────
    print("\n─── Sending to WhatsApp (best-effort) ────────────────────────────")
    files_to_send = [
        (all_file, f"API Recon — All Sources — {REPORT_DATE_STR}"),
        (branded_file, f"API Recon — Branded Campaigns — {REPORT_DATE_STR}"),
    ]
    for fp, cap in files_to_send:
        if fp and os.path.exists(fp):
            send_via_whapi(fp, cap)

    # ── STEP 5: Delivery manifest ────────────────────────────────────────
    manifest = {
        "date": REPORT_DATE_STR,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "files": []
    }
    for fp, cap in files_to_send:
        if fp and os.path.exists(fp):
            manifest["files"].append({
                "path": os.path.abspath(fp),
                "filename": os.path.basename(fp),
                "caption": cap,
                "size_bytes": os.path.getsize(fp)
            })
    manifest_path = os.path.join(OUTPUT_DIR, f"recon_manifest_{RUN_STAMP}.json")
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)
    print(f"\n  📋 Manifest: {manifest_path}")

    print("\n" + "=" * 60)
    print("✅ ALL RECON REPORTS GENERATED")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
