#!/usr/bin/env python3
"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
UNIFIED REGULAR API RECON REPORTS — ALL Sources
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Single script that generates the ALL Sources recon report:
  1. ALL Sources  — Every API recon entry for the day

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

# ─── DATE LOGIC ────────────────────────────────────────────────────────────────
# Usage: python script.py [date YYYY-MM-DD] [cutoff_hour IST (optional)] [start_hour IST (optional)]
#   10 AM  → yesterday full day      → date=yesterday, no cutoff
#   12 PM  → today midnight→12 PM    → date=today,     cutoff=12
#   4 PM   → today 12 PM→4 PM        → date=today,     cutoff=16, start=12
CUTOFF_HOUR = None
START_HOUR = None
if len(sys.argv) > 1:
    REPORT_DATE_STR = sys.argv[1]
    if len(sys.argv) > 2:
        CUTOFF_HOUR = int(sys.argv[2])
    if len(sys.argv) > 3:
        START_HOUR = int(sys.argv[3])
else:
    now_utc = datetime.utcnow()
    now_ist = now_utc + timedelta(hours=5, minutes=30)
    report_date = now_ist - timedelta(days=1) if now_ist.hour < 6 else now_ist
    REPORT_DATE_STR = report_date.strftime('%Y-%m-%d')

_run_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
RUN_STAMP = _run_ist.strftime('%Y-%m-%d_%H-%M')

if CUTOFF_HOUR:
    if START_HOUR:
        DISPLAY_LABEL = f"{REPORT_DATE_STR} ({START_HOUR}:00 → {CUTOFF_HOUR}:00 IST)"
        STATUS_LABEL = f"{REPORT_DATE_STR}_{START_HOUR}IST_to_{CUTOFF_HOUR}IST"
    else:
        DISPLAY_LABEL = f"{REPORT_DATE_STR} (midnight → {CUTOFF_HOUR}:00 IST)"
        STATUS_LABEL = f"{REPORT_DATE_STR}_until_{CUTOFF_HOUR}IST"
else:
    DISPLAY_LABEL = f"{REPORT_DATE_STR} (full day)"
    STATUS_LABEL = f"{REPORT_DATE_STR}_full_day"

print(f"📅 Report Date: {DISPLAY_LABEL}")


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

async def fetch_all_sources(date_str, cutoff_hour=None, start_hour=None):
    """
    Fetch per-college counts segregated by sent_type: bot / auto / manual.
    If cutoff_hour is set (e.g., 12 or 16), only include records up to that hour IST.
    If start_hour is set (e.g., 12), start from that hour IST (default: midnight).
    Otherwise, include the full day.
    """
    conn = await asyncpg.connect(**DB_CONFIG)
    try:
        date_part = datetime.strptime(date_str, '%Y-%m-%d').date()
        if cutoff_hour is not None:
            lower_offset = f'{start_hour} hours' if start_hour is not None else '0 hours'
            upper_offset = f'{cutoff_hour} hours'
            rows = await conn.fetch(f"""
                SELECT
                    college_name,
                    COUNT(DISTINCT CASE WHEN sent_type='bot' AND api_sent_status='Submitted via Bot (Direct Portal)' THEN student_id END) AS bot_submitted,
                    COUNT(DISTINCT CASE WHEN sent_type='bot' AND api_sent_status='Failed due to Technical Issues'    THEN student_id END) AS bot_fail,
                    COUNT(DISTINCT CASE WHEN sent_type='auto' AND api_sent_status='Proceed'                                              THEN student_id END) AS auto_proceed,
                    COUNT(DISTINCT CASE WHEN sent_type='auto' AND api_sent_status='Failed due to Technical Issues'                       THEN student_id END) AS auto_fail,
                    COUNT(DISTINCT CASE WHEN sent_type='auto' AND api_sent_status IN ('Do not Proceed','Do not Proceed (Still) ')         THEN student_id END) AS auto_dnp,
                    COUNT(DISTINCT CASE WHEN sent_type='manual' AND api_sent_status='Proceed'                                            THEN student_id END) AS manual_proceed,
                    COUNT(DISTINCT CASE WHEN sent_type='manual' AND api_sent_status='Failed due to Technical Issues'                     THEN student_id END) AS manual_fail,
                    COUNT(DISTINCT CASE WHEN sent_type='manual' AND api_sent_status IN ('Do not Proceed','Do not Proceed (Still) ')       THEN student_id END) AS manual_dnp,
                    COUNT(DISTINCT CASE WHEN sent_type IN ('auto','manual') AND api_sent_status='Proceed'                                THEN student_id END) AS total_proceed,
                    COUNT(DISTINCT CASE WHEN sent_type IN ('auto','manual') AND api_sent_status='Failed due to Technical Issues'         THEN student_id END) AS total_fail,
                    COUNT(DISTINCT CASE WHEN sent_type IN ('auto','manual') AND api_sent_status IN ('Do not Proceed','Do not Proceed (Still) ') THEN student_id END) AS total_dnp
                FROM student_college_api_sent_status
                WHERE created_at >= $1::date - interval '5 hours 30 minutes' + interval '{lower_offset}'
                  AND created_at <  $1::date - interval '5 hours 30 minutes' + interval '{upper_offset}'
                GROUP BY college_name
                ORDER BY college_name
            """, date_part)
        else:
            rows = await conn.fetch(f"""
                SELECT
                    college_name,
                    COUNT(DISTINCT CASE WHEN sent_type='bot' AND api_sent_status='Submitted via Bot (Direct Portal)' THEN student_id END) AS bot_submitted,
                    COUNT(DISTINCT CASE WHEN sent_type='bot' AND api_sent_status='Failed due to Technical Issues'    THEN student_id END) AS bot_fail,
                    COUNT(DISTINCT CASE WHEN sent_type='auto' AND api_sent_status='Proceed'                                              THEN student_id END) AS auto_proceed,
                    COUNT(DISTINCT CASE WHEN sent_type='auto' AND api_sent_status='Failed due to Technical Issues'                       THEN student_id END) AS auto_fail,
                    COUNT(DISTINCT CASE WHEN sent_type='auto' AND api_sent_status IN ('Do not Proceed','Do not Proceed (Still) ')         THEN student_id END) AS auto_dnp,
                    COUNT(DISTINCT CASE WHEN sent_type='manual' AND api_sent_status='Proceed'                                            THEN student_id END) AS manual_proceed,
                    COUNT(DISTINCT CASE WHEN sent_type='manual' AND api_sent_status='Failed due to Technical Issues'                     THEN student_id END) AS manual_fail,
                    COUNT(DISTINCT CASE WHEN sent_type='manual' AND api_sent_status IN ('Do not Proceed','Do not Proceed (Still) ')       THEN student_id END) AS manual_dnp,
                    COUNT(DISTINCT CASE WHEN sent_type IN ('auto','manual') AND api_sent_status='Proceed'                                THEN student_id END) AS total_proceed,
                    COUNT(DISTINCT CASE WHEN sent_type IN ('auto','manual') AND api_sent_status='Failed due to Technical Issues'         THEN student_id END) AS total_fail,
                    COUNT(DISTINCT CASE WHEN sent_type IN ('auto','manual') AND api_sent_status IN ('Do not Proceed','Do not Proceed (Still) ') THEN student_id END) AS total_dnp
                FROM student_college_api_sent_status
                WHERE created_at >= $1::date - interval '5 hours 30 minutes'
                  AND created_at <  $1::date + interval '1 day' - interval '5 hours 30 minutes'
                GROUP BY college_name
                ORDER BY college_name
            """, date_part)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# DATA TRANSFORM
# ═══════════════════════════════════════════════════════════════════════════════

def transform_to_matrix(rows):
    """
    Rows already come deduplicated from DB.
    Returns {college: {auto_manual_proceed, auto_manual_fail, auto_manual_dnp,
                        bot_submitted, bot_fail}}
    """
    return {r['college_name']: dict(r) for r in rows}


# ═══════════════════════════════════════════════════════════════════════════════
# HTML GENERATION (Dark Theme with Barlow font)
# ═══════════════════════════════════════════════════════════════════════════════

def _v(n):
    """Return number or em-dash for zero."""
    return '&mdash;' if not n else str(n)


def generate_html(matrix, report_date, title, filename):
    """
    Column order: Bot (Submitted, Fail) | Auto (Proceed, Fail, DNP) |
                  Manual (Proceed, Fail, DNP) | Total (Proceed, Fail, DNP)
    No Total Leads column.
    """
    colleges = sorted(matrix.keys())

    dt = datetime.strptime(report_date, '%Y-%m-%d')
    try:
        date_display = dt.strftime('%#d %B %Y')
    except Exception:
        date_display = dt.strftime('%d %B %Y').lstrip('0') or dt.strftime('%d %B %Y')

    badge = 'All Sources'
    rows_html = ''
    gt = {k: 0 for k in ('bot_s','bot_f','ap','af','ad','mp','mf','md','tp','tf','td')}

    for college in colleges:
        d = matrix[college]
        g = lambda k: d.get(k, 0) or 0

        bot_s = g('bot_submitted');  bot_f = g('bot_fail')
        ap = g('auto_proceed');      af = g('auto_fail');   ad = g('auto_dnp')
        mp = g('manual_proceed');    mf = g('manual_fail'); md = g('manual_dnp')
        # Total = auto+manual (deduped)
        tp = g('total_proceed')
        tf = g('total_fail')
        td = g('total_dnp')

        for k, v in zip(('bot_s','bot_f','ap','af','ad','mp','mf','md','tp','tf','td'),
                        ( bot_s,  bot_f,  ap,  af,  ad,  mp,  mf,  md,  tp,  tf,  td)):
            gt[k] += v

        rows_html += f"""<tr>
<td class=td-college>{college}</td>
<td class=bot-sub>{_v(bot_s)}</td><td class="fail bl-bot">{_v(bot_f)}</td>
<td class="t-proc bl-auto">{_v(ap)}</td><td class=fail>{_v(af)}</td><td class=dnp>{_v(ad)}</td>
<td class="t-proc bl-man">{_v(mp)}</td><td class=fail>{_v(mf)}</td><td class=dnp>{_v(md)}</td>
<td class="t-proc bl-tot">{_v(tp)}</td><td class=fail>{_v(tf)}</td><td class=dnp>{_v(td)}</td>
</tr>\n"""

    grand_row = f"""<tr class=grand>
<td class=td-college>Grand Total</td>
<td class=bot-sub>{_v(gt['bot_s'])}</td><td class="fail bl-bot">{_v(gt['bot_f'])}</td>
<td class="t-proc bl-auto">{_v(gt['ap'])}</td><td class=fail>{_v(gt['af'])}</td><td class=dnp>{_v(gt['ad'])}</td>
<td class="t-proc bl-man">{_v(gt['mp'])}</td><td class=fail>{_v(gt['mf'])}</td><td class=dnp>{_v(gt['md'])}</td>
<td class="t-proc bl-tot">{_v(gt['tp'])}</td><td class=fail>{_v(gt['tf'])}</td><td class=dnp>{_v(gt['td'])}</td>
</tr>\n"""

    grand_total = gt['tp'] + gt['tf'] + gt['td']

    summary_row = f"""<tr class=summary>
<td class=sum-label>Summary Total</td>
<td colspan=2 class=s-bot>{gt['bot_s'] + gt['bot_f']}</td>
<td colspan=3 class=s-auto>{gt['ap'] + gt['af'] + gt['ad']}</td>
<td colspan=3 class=s-man>{gt['mp'] + gt['mf'] + gt['md']}</td>
<td colspan=3 class=s-tot>{gt['tp'] + gt['tf'] + gt['td']}</td>
</tr>\n"""

    css = """@import url('https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;600;700;800&family=Barlow+Condensed:wght@600;700;800&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:#060e18;min-height:100%}
body{padding:32px 24px;font-family:'Barlow',sans-serif}
.rr-root{background:linear-gradient(160deg,#0b1a2e 0%,#081420 100%);border-radius:18px;padding:36px 40px;max-width:1400px;margin:0 auto;box-shadow:0 8px 48px rgba(0,0,0,.6);border:1px solid #12263d}
.rr-top-badge{display:inline-block;background:linear-gradient(90deg,#0d3a5c,#0e4d7a);color:#39b8f5;font-size:10px;font-weight:800;letter-spacing:.18em;text-transform:uppercase;padding:5px 14px;border-radius:6px;margin-bottom:12px;border:1px solid #1a6a9a;box-shadow:0 0 12px rgba(57,184,245,.15)}
.rr-header-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:32px;gap:16px}
.rr-title{font-family:'Barlow Condensed',sans-serif;font-size:34px;font-weight:800;color:#39b8f5;letter-spacing:.05em;text-transform:uppercase;line-height:1.1;text-shadow:0 0 30px rgba(57,184,245,.3)}
.rr-date-box{background:linear-gradient(135deg,#0e2035,#111e30);border:1px solid #1e4060;border-radius:10px;padding:10px 22px;font-size:15px;font-weight:700;color:#7faec9;white-space:nowrap;box-shadow:inset 0 1px 0 rgba(255,255,255,.05)}
.rr-table-wrap{border-radius:12px;overflow:hidden;border:1px solid #162840;box-shadow:0 4px 24px rgba(0,0,0,.4)}
table{width:100%;border-collapse:collapse}
th{text-align:center;padding:0}
.th-college{text-align:left;padding:16px 18px;font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:#3d6a88;background:#09192a}
.th-bot {padding:14px 8px;font-size:10px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;background:linear-gradient(180deg,#1a1000,#120b00);color:#f5a623;border-left:3px solid #5a3a00;text-shadow:0 0 10px rgba(245,166,35,.3)}
.th-auto{padding:14px 8px;font-size:10px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;background:linear-gradient(180deg,#091e32,#071525);color:#39b8f5;border-left:3px solid #1a4060;text-shadow:0 0 10px rgba(57,184,245,.2)}
.th-man {padding:14px 8px;font-size:10px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;background:linear-gradient(180deg,#120e2a,#0d0a20);color:#a78bfa;border-left:3px solid #3a2070;text-shadow:0 0 10px rgba(167,139,250,.2)}
.th-tot {padding:14px 8px;font-size:10px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;background:linear-gradient(180deg,#081a10,#051209);color:#3ddc84;border-left:3px solid #1a5030;text-shadow:0 0 10px rgba(61,220,132,.2)}
.th-sub{font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;padding:9px 5px;border-top:1px solid #0e2035}
.th-sub.s-bot {color:#f5a623;background:#0f0900;border-left:3px solid #5a3a00}
.th-sub.s-auto{color:#39b8f5;background:#061220;border-left:3px solid #1a4060}
.th-sub.s-man {color:#a78bfa;background:#090619;border-left:3px solid #3a2070}
.th-sub.s-tot {color:#3ddc84;background:#040e08;border-left:3px solid #1a5030}
.th-sub.fail{color:#e05c5c}.th-sub.dnp{color:#5a7a90}
td{text-align:center;padding:15px 5px;font-size:15px;font-weight:600;color:#9ab8cc;background:#07111e;border-top:1px solid #0e2035}
td.td-college{text-align:left;padding:15px 18px;font-size:14px;font-weight:700;color:#cce0f0;background:#07111e;border-right:1px solid #0e2035}
td.fail{color:#e05c5c;font-weight:600}
td.dnp{color:#5a7a90}
td.bot-sub{color:#f5a623;font-weight:700;background:#0a0800}
td.t-proc{color:#39b8f5;font-weight:700;background:#06101a}
td.bl-bot {border-left:3px solid #5a3a00;background:#0a0800}
td.bl-man {border-left:3px solid #3a2070;background:#08061a}
td.bl-tot {border-left:3px solid #1a5030;background:#04100a}
td.bl-auto{border-left:3px solid #1a4060;background:#06101a}
tr:hover td{filter:brightness(1.12)}
tr.grand td{border-top:3px solid #1c3d5a;font-weight:800;font-size:16px}
tr.grand td.td-college{color:#39b8f5;background:#0a1e35;font-size:14px}
tr.grand td{color:#c0d8ec;background:#081828}
tr.grand td.bot-sub{color:#f5a623;background:#100e00}
tr.grand td.t-proc{color:#39b8f5;background:#061828}
tr.grand td.fail{color:#ff7070}
tr.grand td.dnp{color:#6a8fa8}
tr.grand td.bl-bot {background:#100e00;border-left:3px solid #5a3a00}
tr.grand td.bl-auto{background:#061828;border-left:3px solid #1a4060}
tr.grand td.bl-man {background:#0a0820;border-left:3px solid #3a2070}
tr.grand td.bl-tot {background:#041408;border-left:3px solid #1a5030}
tr.summary td{border-top:3px solid #1e5a82;padding:17px 8px;font-size:18px;font-weight:800}
td.sum-label{text-align:left;padding-left:18px;font-size:14px;font-weight:700;color:#7faec9;background:#091828;border-right:1px solid #0e2035}
td.s-bot {background:#160d00;color:#f5a623;border-left:3px solid #5a3a00;font-size:20px}
td.s-auto{background:#071828;color:#39b8f5;border-left:3px solid #1a4060;font-size:20px}
td.s-man {background:#0b0820;color:#a78bfa;border-left:3px solid #3a2070;font-size:20px}
td.s-tot {background:#040e06;color:#3ddc84;border-left:3px solid #1a5030;font-size:20px}
.rr-note{margin-top:16px;font-size:12px;color:#2a4a60;text-align:right;letter-spacing:.05em}"""

    html_doc = f"""<!DOCTYPE html><html><head><meta charset=UTF-8>
<style>{css}</style></head><body><div class=rr-root>
<div class=rr-header-row>
  <div><div class=rr-top-badge>All Sources</div><div class=rr-title>Regular API Recon Report</div></div>
  <div class=rr-date-box>{date_display}</div>
</div>
<div class=rr-table-wrap><table>
<thead>
<tr>
  <th class=th-college rowspan=2>College Name</th>
  <th class=th-bot  colspan=2>Bot</th>
  <th class=th-auto colspan=3>Auto Recon</th>
  <th class=th-man  colspan=3>Manual Recon</th>
  <th class=th-tot  colspan=3>Total</th>
</tr>
<tr>
  <th class="th-sub s-bot">Submitted</th><th class="th-sub fail">Fail</th>
  <th class="th-sub s-auto">Proceed</th><th class="th-sub fail">Fail</th><th class="th-sub dnp">DNP</th>
  <th class="th-sub s-man">Proceed</th><th class="th-sub fail">Fail</th><th class="th-sub dnp">DNP</th>
  <th class="th-sub s-tot">Proceed</th><th class="th-sub fail">Fail</th><th class="th-sub dnp">DNP</th>
</tr>
</thead>
<tbody>{rows_html}{grand_row}{summary_row}</tbody>
</table></div>
<div class=rr-note>{grand_total} total records | {date_display}</div>
</div></body></html>"""

    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(html_doc)

    stats = {
        'bot_submitted': gt['bot_s'], 'bot_fail': gt['bot_f'],
        'auto_proceed': gt['ap'], 'auto_fail': gt['af'], 'auto_dnp': gt['ad'],
        'manual_proceed': gt['mp'], 'manual_fail': gt['mf'], 'manual_dnp': gt['md'],
        'total_proceed': gt['tp'], 'total_fail': gt['tf'], 'total_dnp': gt['td'],
        'colleges': len(colleges), 'filepath': filepath
    }

    print(f"  ✅ Saved: {filename} ({grand_total} total, {len(colleges)} colleges)")
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
    print("📊 REGULAR API RECON REPORTS — ALL Sources")
    print("=" * 60)
    print(f"   Date      : {DISPLAY_LABEL}")
    print(f"   Start Hour: {START_HOUR}")
    print(f"   Cutoff    : {CUTOFF_HOUR}")
    print(f"   Python    : {sys.executable}")
    print(f"   CWD       : {os.getcwd()}")
    print(f"   Base Dir  : {BASE_DIR}")

    # ── CHECK ENV ────────────────────────────────────────────────────────
    print("\n─── Environment Check ─────────────────────────────────────────")
    for key in ('REGULAR_LMS_DB_HOST', 'REGULAR_LMS_DB_PORT', 'REGULAR_LMS_DB_NAME',
                'REGULAR_LMS_DB_USER', 'REGULAR_LMS_DB_PASSWORD', 'WHAPI_TOKEN',
                'WHATSAPP_GROUP', 'GOOGLE_TOKEN_JSON', 'GOOGLE_CLIENT_SECRET_JSON'):
        val = os.getenv(key)
        if val:
            masked = val[:6] + '...' + val[-4:] if len(val) > 12 else '***'
            print(f"   ✓  {key} = {masked}")
        else:
            print(f"   ✗  {key} = NOT SET")

    # ── STEP 1: ALL Sources ──────────────────────────────────────────────
    print("\n─── ALL Sources ─────────────────────────────────────────────────")
    print(f"   Calling fetch_all_sources(date={REPORT_DATE_STR}, cutoff={CUTOFF_HOUR}, start={START_HOUR})")
    try:
        all_rows = await fetch_all_sources(REPORT_DATE_STR, CUTOFF_HOUR, START_HOUR)
    except Exception as e:
        import traceback
        print(f"❌  DB QUERY FAILED: {e}")
        traceback.print_exc()
        sys.exit(1)

    print(f"  Rows: {len(all_rows)}")
    for r in all_rows:
        print(f"    {r['college_name']} | bot={r['bot_submitted']}/{r['bot_fail']} auto={r['auto_proceed']}/{r['auto_fail']}/{r['auto_dnp']} manual={r['manual_proceed']}/{r['manual_fail']}/{r['manual_dnp']} total={r['total_proceed']}/{r['total_fail']}/{r['total_dnp']}")

    all_matrix = transform_to_matrix(all_rows)
    print(f"  Colleges: {len(all_matrix)}")

    try:
        all_file, all_stats = generate_html(
            all_matrix, REPORT_DATE_STR,
            f"API Recon — All Sources — {DISPLAY_LABEL}",
            f"Regular_Recon_All_{STATUS_LABEL}_{RUN_STAMP}.html"
        )
        print(f"  HTML written to: {all_file}")
    except Exception as e:
        import traceback
        print(f"❌  HTML GENERATION FAILED: {e}")
        traceback.print_exc()
        sys.exit(1)

    # ── STEP 2: Summary ──────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("SUMMARY STATS")
    print("=" * 60)
    print(f"\n  ALL SOURCES — {DISPLAY_LABEL}")
    for k, v in all_stats.items():
        if k != 'filepath':
            print(f"    {k}: {v}")

    # ── STEP 3: WHAPI send (best-effort) ─────────────────────────────────
    print("\n─── Sending to WhatsApp (best-effort) ────────────────────────────")
    files_to_send = [
        (all_file, f"API Recon — All Sources — {DISPLAY_LABEL}"),
    ]
    whapi_ok = 0
    whapi_fail = 0
    for fp, cap in files_to_send:
        if fp and os.path.exists(fp):
            print(f"   Sending: {os.path.basename(fp)}  ({os.path.getsize(fp)} bytes)")
            if send_via_whapi(fp, cap):
                whapi_ok += 1
            else:
                whapi_fail += 1
        else:
            print(f"   SKIP — file not found: {fp}")
            whapi_fail += 1
    print(f"   WHAPI results: {whapi_ok} sent, {whapi_fail} failed/skipped")

    # ── STEP 4: Delivery manifest ────────────────────────────────────────
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
    print(f"  📋 Files in manifest: {len(manifest['files'])}")

    # ── STEP 5: Cleanup output folder ────────────────────────────────────
    import shutil
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

    print("\n" + "=" * 60)
    print("✅ ALL RECON REPORTS GENERATED")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
