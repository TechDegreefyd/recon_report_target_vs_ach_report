#!/usr/bin/env python3
"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ADMISSION LEDGER REPORTS — Daily + Month-to-Date
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Builds the two "Admission Ledger" reports (team owner -> counsellor breakdown
of admissions by fee type) and sends them to WhatsApp as screenshots:
  1. Daily   — admissions closed on the report date only
  2. MTD     — admissions closed from the 1st of the month through the report date

Query source: new_report_queries.txt (parametrized here with a date window
instead of hardcoded dates).

Usage:  python generate_admission_ledger_reports.py [--date YYYY-MM-DD] [--local] [--nocleanup]
Environment:  .env in this folder (ONLINE_LMS_DB_*, WHAPI_TOKEN_PAID,
              WHATSAPP_GROUP_ADMISSION_LEDGER or WHATSAPP_GROUP)
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
from datetime import datetime, timedelta, UTC
from dotenv import load_dotenv

try:
    import asyncpg
    import requests
except ImportError as e:
    print(f"❌ Missing dependency: {e}")
    print("   Run: pip install asyncpg requests python-dotenv")
    sys.exit(1)

# ─── PATHS ──────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))

OUTPUT_DIR = os.path.join(BASE_DIR, 'Automation Cron Job', 'Admission Ledger')
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ─── WHAPI ──────────────────────────────────────────────────────────────────────
WHAPI_TOKEN = os.getenv('WHAPI_TOKEN_PAID')
WHATSAPP_GROUP = [g.strip() for g in os.getenv(
    'WHATSAPP_GROUP_ADMISSION_LEDGER', os.getenv('WHATSAPP_GROUP', '120363426619711887@g.us')
).split(',') if g.strip()]

# ─── DB ─────────────────────────────────────────────────────────────────────────
DB_CONFIG = {
    "host": os.getenv("ONLINE_LMS_DB_HOST"),
    "port": int(os.getenv("ONLINE_LMS_DB_PORT", "54321")),
    "database": os.getenv("ONLINE_LMS_DB_NAME"),
    "user": os.getenv("ONLINE_LMS_DB_USER"),
    "password": os.getenv("ONLINE_LMS_DB_PASSWORD"),
}

# ─── ARGS ───────────────────────────────────────────────────────────────────────
LOCAL_MODE = '--local' in sys.argv
NO_CLEANUP = '--nocleanup' in sys.argv

REPORT_DATE_STR = None
for i, a in enumerate(sys.argv):
    if a == '--date' and i + 1 < len(sys.argv):
        REPORT_DATE_STR = sys.argv[i + 1]

if not REPORT_DATE_STR:
    now_ist = datetime.now(UTC) + timedelta(hours=5, minutes=30)
    REPORT_DATE_STR = now_ist.strftime('%Y-%m-%d')

_run_ist = datetime.now(UTC) + timedelta(hours=5, minutes=30)
RUN_STAMP = _run_ist.strftime('%Y-%m-%d_%H-%M')

REPORT_DATE = datetime.strptime(REPORT_DATE_STR, '%Y-%m-%d').date()
MONTH_START = REPORT_DATE.replace(day=1)

# ─── Day-boundary cutoff ──────────────────────────────────────────────────────
# Admissions are cut off at 8:30 PM IST — anything marked after 8:30 PM rolls into
# the NEXT day's daily/monthly window instead of the current one.
IST_OFFSET   = timedelta(hours=5, minutes=30)
CUTOFF_TIME  = timedelta(hours=20, minutes=30)


def cutoff_instant(d):
    """Naive UTC instant for the 8:30 PM IST cutoff on calendar date d."""
    return datetime(d.year, d.month, d.day) + CUTOFF_TIME - IST_OFFSET


print(f"📅 Report Date: {REPORT_DATE_STR}  (day boundary = 8:30 PM IST)")


# ═══════════════════════════════════════════════════════════════════════════════
# DB QUERY
# ═══════════════════════════════════════════════════════════════════════════════

ADMISSION_LEDGER_QUERY = """
WITH admission_events AS (
  SELECT student_id, course_id, created_at, INITCAP(TRIM(fee_type)) AS fee_type_clean
  FROM course_status_journeys
  WHERE course_status = 'Admission'
    AND student_id IN (SELECT student_id FROM students)
),
deduped AS (
  SELECT DISTINCT ON (student_id, course_id) student_id, course_id, created_at, fee_type_clean
  FROM admission_events
  ORDER BY student_id, course_id, created_at ASC
),
window_filtered AS (
  SELECT * FROM deduped
  WHERE created_at >= $1::timestamp
    AND created_at < $2::timestamp
),
counsellor_admissions AS (
  SELECT s.assigned_counsellor_id, d.fee_type_clean
  FROM window_filtered d JOIN students s ON s.student_id = d.student_id
)
SELECT COALESCE(to_c.counsellor_name,'Unassigned Team') AS team_owner,
       l2.counsellor_name AS counsellor_name,
       COUNT(*) AS total_admissions,
       COUNT(*) FILTER (WHERE ca.fee_type_clean IN ('Partial Paid','Partially Paid','Partial Done')) AS partial_paid,
       COUNT(*) FILTER (WHERE ca.fee_type_clean ILIKE '%Annual%') AS annual_paid,
       COUNT(*) FILTER (WHERE ca.fee_type_clean ILIKE '%Semester%') AS semester_paid,
       COUNT(*) FILTER (WHERE ca.fee_type_clean ILIKE '%Full%') AS full_paid,
       COUNT(*) FILTER (WHERE ca.fee_type_clean NOT ILIKE '%Annual%' AND ca.fee_type_clean NOT ILIKE '%Semester%'
                          AND ca.fee_type_clean NOT ILIKE '%Full%'
                          AND ca.fee_type_clean NOT IN ('Partial Paid','Partially Paid','Partial Done')) AS other_unspecified
FROM counsellor_admissions ca
JOIN counsellors l2 ON l2.counsellor_id = ca.assigned_counsellor_id
LEFT JOIN counsellors to_c ON to_c.counsellor_id = l2.assigned_to
GROUP BY to_c.counsellor_name, l2.counsellor_name
ORDER BY to_c.counsellor_name NULLS LAST, total_admissions DESC
"""


async def fetch_admission_ledger(start_dt, end_dt_exclusive):
    conn = await asyncpg.connect(**DB_CONFIG)
    try:
        rows = await conn.fetch(ADMISSION_LEDGER_QUERY, start_dt, end_dt_exclusive)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# HTML GENERATION (Fraunces / IBM Plex Mono "ledger" theme)
# ═══════════════════════════════════════════════════════════════════════════════

LEDGER_CSS = """
:root{
  --paper:#ffffff; --paper-tint:#faf8f3; --ink:#1c1a16; --ink-soft:#6b6558;
  --gold:#b8863d; --gold-deep:#8f6a2e; --rule:#e6ddc9;
  --coral:#b5563f; --forest:#3f6b52; --slate:#3d5a73; --plum:#6b4d7a;
}
*{box-sizing:border-box;}
body{margin:0;padding:30px 22px 44px;background:var(--paper-tint);font-family:'IBM Plex Mono', monospace;color:var(--ink);}
.masthead{max-width:1180px;margin:0 auto 24px;border-bottom:2px solid var(--gold);padding-bottom:16px;display:flex;justify-content:space-between;align-items:flex-end;gap:20px;flex-wrap:wrap;}
.title-block{display:flex;flex-direction:column;gap:4px;}
.eyebrow{font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--gold-deep);}
h1{font-family:'Fraunces', serif;font-weight:600;font-size:32px;margin:0;color:var(--ink);letter-spacing:-.01em;}
.meta{font-size:11.5px;color:var(--ink-soft);line-height:1.6;}
.meta b{color:var(--ink);}
.stamp{font-family:'Fraunces', serif;font-weight:700;font-size:13px;border:1.5px solid var(--gold);color:var(--gold-deep);padding:7px 14px;border-radius:2px;letter-spacing:.05em;transform:rotate(-2deg);white-space:nowrap;}
.grid{max-width:1180px;margin:0 auto;display:grid;grid-template-columns:1fr 1fr;gap:18px;}
@media(max-width:900px){.grid{grid-template-columns:1fr;}}
.ledger-card{background:var(--paper);border:1px solid var(--rule);border-radius:4px;box-shadow:0 4px 14px -8px rgba(28,26,22,.18);overflow:hidden;position:relative;}
.ledger-card::before{content:"";position:absolute;top:0;left:0;right:0;height:4px;background:var(--gold);}
.card-head{display:flex;justify-content:space-between;align-items:baseline;padding:16px 18px 10px;border-bottom:1px dashed var(--rule);}
.card-head .team{font-family:'Fraunces', serif;font-weight:600;font-size:19px;color:var(--ink);}
.card-head .rank{font-size:10px;color:var(--gold-deep);letter-spacing:.15em;text-transform:uppercase;}
.card-head .team-total{font-family:'Fraunces', serif;font-weight:700;font-size:24px;color:var(--coral);text-align:right;}
.card-head .team-total sub{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--ink-soft);letter-spacing:.05em;text-transform:uppercase;display:block;margin-top:-2px;}
table{width:100%;border-collapse:collapse;font-size:11.5px;}
thead th{text-align:left;padding:8px 18px 6px;font-size:9.5px;color:var(--ink-soft);text-transform:uppercase;letter-spacing:.08em;font-weight:600;border-bottom:1px solid var(--rule);}
thead th.num{text-align:center;}
tbody td{padding:6px 18px;border-bottom:1px solid #f0ead9;}
tbody tr:last-child td{border-bottom:none;}
tbody td.name{font-weight:500;color:var(--ink);}
tbody td.num{text-align:center;color:var(--ink-soft);}
tbody tr:nth-child(even){background:rgba(184,134,61,.05);}
.pip{display:inline-block;min-width:18px;padding:1px 6px;border-radius:9px;font-size:10.5px;font-weight:600;font-family:'IBM Plex Mono',monospace;}
.pip.total{background:var(--ink);color:#fff;}
.pip.partial{background:rgba(181,86,63,.12);color:var(--coral);}
.pip.annual{background:rgba(63,107,82,.12);color:var(--forest);}
.pip.sem{background:rgba(61,90,115,.12);color:var(--slate);}
.pip.full{background:rgba(107,77,122,.12);color:var(--plum);}
.dash{color:#c9c2ae;}
.team-foot{display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px;padding:9px 18px;background:var(--paper-tint);font-size:10.5px;letter-spacing:.03em;border-top:1px solid var(--rule);}
.team-foot span{color:var(--ink-soft);}
.team-foot b{color:var(--gold-deep);margin-left:5px;}
.legend{max-width:1180px;margin:26px auto 0;display:flex;gap:16px;flex-wrap:wrap;font-size:10.5px;color:var(--ink-soft);}
.legend span::before{content:"● ";}
.legend .l-partial::before{color:var(--coral);}
.legend .l-annual::before{color:var(--forest);}
.legend .l-sem::before{color:var(--slate);}
.legend .l-full::before{color:var(--plum);}
.empty-note{max-width:1180px;margin:8px auto 0;font-size:11px;color:var(--ink-soft);text-align:center;}
"""


def _day_fmt():
    return '%#d' if sys.platform == 'win32' else '%-d'


def _fmt_date_display(d):
    return d.strftime(f'{_day_fmt()} %b %Y')


def build_ledger_html(rows, *, title, stamp_label, note, page_title):
    """
    rows: list of dicts with team_owner, counsellor_name, total_admissions,
          partial_paid, annual_paid, semester_paid, full_paid
    Renders the same static shell as system_1 (1)/(2).html, with data injected
    as JSON and rendered client-side by the same JS (so screenshots match
    exactly what was hand-designed).
    """
    data_json = json.dumps(rows, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{page_title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600;9..144,700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>{LEDGER_CSS}</style>
</head>
<body>

<div class="masthead">
  <div class="title-block">
    <span class="eyebrow">DegreeFYD · Admissions Desk</span>
    <h1>{title}</h1>
    <div class="meta">Course status = <b>Admission</b>, deduped first-hit per student/course · grouped Team Owner → Counsellor</div>
  </div>
  <div class="stamp">{stamp_label}</div>
</div>

<div class="grid" id="grid"></div>

<div class="legend">
  <span class="l-partial">Partial Paid</span>
  <span class="l-annual">Annual Paid</span>
  <span class="l-sem">Semester Paid</span>
  <span class="l-full">Full Paid</span>
</div>
<div class="empty-note">{note}</div>

<script>
const data = {data_json};

const teams = {{}};
data.forEach(r=>{{ (teams[r.team_owner] = teams[r.team_owner]||[]).push(r); }});

const grid = document.getElementById('grid');

const orderedTeams = Object.keys(teams).sort((a,b)=>{{
  const ta = teams[a].reduce((s,r)=>s+r.total_admissions,0);
  const tb = teams[b].reduce((s,r)=>s+r.total_admissions,0);
  return tb-ta;
}});

orderedTeams.forEach((team, idx)=>{{
  const rows = teams[team].sort((a,b)=>b.total_admissions-a.total_admissions);
  const t = rows.reduce((acc,r)=>{{
    acc.total+=r.total_admissions; acc.partial+=r.partial_paid; acc.annual+=r.annual_paid;
    acc.sem+=r.semester_paid; acc.full+=r.full_paid; return acc;
  }},{{total:0,partial:0,annual:0,sem:0,full:0}});

  const pip = (val, cls) => val ? `<span class="pip ${{cls}}">${{val}}</span>` : `<span class="dash">–</span>`;

  const card = document.createElement('div');
  card.className='ledger-card';
  card.innerHTML = `
    <div class="card-head">
      <div>
        <div class="rank">Rank ${{String(idx+1).padStart(2,'0')}} of ${{orderedTeams.length}}</div>
        <div class="team">${{team}}</div>
      </div>
      <div class="team-total">${{t.total}}<sub>admissions</sub></div>
    </div>
    <table>
      <thead><tr>
        <th>Counsellor</th><th class="num">Total</th><th class="num">Partial</th><th class="num">Annual</th><th class="num">Semester</th><th class="num">Full</th>
      </tr></thead>
      <tbody>
        ${{rows.map(r=>`
        <tr>
          <td class="name">${{r.counsellor_name}}</td>
          <td class="num">${{pip(r.total_admissions,'total')}}</td>
          <td class="num">${{pip(r.partial_paid,'partial')}}</td>
          <td class="num">${{pip(r.annual_paid,'annual')}}</td>
          <td class="num">${{pip(r.semester_paid,'sem')}}</td>
          <td class="num">${{pip(r.full_paid,'full')}}</td>
        </tr>`).join('')}}
      </tbody>
    </table>
    <div class="team-foot">
      <span>${{rows.length}} counsellor${{rows.length>1?'s':''}} closed</span>
      <span>Partial <b>${{t.partial}}</b> &nbsp; Annual <b>${{t.annual}}</b> &nbsp; Semester <b>${{t.sem}}</b> &nbsp; Full <b>${{t.full}}</b></span>
    </div>`;
  grid.appendChild(card);
}});
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# SCREENSHOT (Playwright headless Chromium)
# ═══════════════════════════════════════════════════════════════════════════════

async def screenshot_html(html_path, png_path, viewport_width=1240):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("  ⚠️  playwright not installed — run: pip install playwright && playwright install chromium")
        return False

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=['--no-sandbox', '--disable-setuid-sandbox'])
            page = await browser.new_page(viewport={'width': viewport_width, 'height': 900})
            await page.goto(f'file:///{os.path.abspath(html_path)}', wait_until='networkidle', timeout=30000)
            await page.wait_for_timeout(400)
            await page.screenshot(path=png_path, full_page=True)
            await browser.close()
        print(f"  ✅ Screenshot saved: {os.path.basename(png_path)}")
        return True
    except Exception as e:
        print("  ⚠️  Screenshot failed: " + str(e))
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# WHAPI SEND
# ═══════════════════════════════════════════════════════════════════════════════

def send_via_whapi(file_path, caption):
    if not WHAPI_TOKEN:
        print(f"  ⚠️  WHAPI_TOKEN_PAID not set — file kept at: {file_path}")
        return False

    filename = os.path.basename(file_path)
    with open(file_path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode('utf-8')

    media_data = f'data:image/png;name={filename};base64,{b64}'
    headers = {'accept': 'application/json', 'authorization': f'Bearer {WHAPI_TOKEN}',
               'content-type': 'application/json'}
    results = []
    for gid in WHATSAPP_GROUP:
        payload = {'to': gid, 'media': media_data, 'caption': caption}
        for attempt in range(2):
            try:
                r = requests.post('https://gate.whapi.cloud/messages/image',
                                  headers=headers, json=payload, timeout=20)
                if 200 <= r.status_code < 300:
                    print(f"  ✅ WHAPI sent: {filename} → {gid}")
                    results.append(True)
                    break
                print(f"  ⚠️  WHAPI HTTP {r.status_code}: {r.text[:120]}")
                results.append(False)
                break
            except requests.exceptions.Timeout:
                print(f"  ⚠️  WHAPI timeout (network unreachable?)")
                results.append(False)
                break
            except Exception as e:
                print(f"  ⚠️  WHAPI error: {e}")
                if attempt == 0:
                    time.sleep(3)
                else:
                    results.append(False)
    return all(results)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

async def build_and_send(rows, *, title, stamp_label, note, page_title, filename_stub, caption):
    html_doc = build_ledger_html(rows, title=title, stamp_label=stamp_label, note=note, page_title=page_title)
    html_path = os.path.join(OUTPUT_DIR, f"{filename_stub}_{RUN_STAMP}.html")
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html_doc)
    print(f"  HTML written to: {html_path}")

    png_path = os.path.join(OUTPUT_DIR, f"{filename_stub}_{RUN_STAMP}.png")
    screenshot_ok = await screenshot_html(html_path, png_path)

    if LOCAL_MODE:
        print("  ⏭️  LOCAL_MODE — skipping WhatsApp send")
    elif screenshot_ok:
        send_via_whapi(png_path, caption)
    else:
        print("  ⚠️  Screenshot failed — nothing sent to WhatsApp")

    return html_path, png_path, screenshot_ok


async def main():
    print("=" * 60)
    print("📊 ADMISSION LEDGER REPORTS — Daily + MTD")
    print("=" * 60)
    print(f"   Date      : {REPORT_DATE_STR}")
    print(f"   Python    : {sys.executable}")
    print(f"   Base Dir  : {BASE_DIR}")

    print("\n─── Environment Check ─────────────────────────────────────────")
    for key in ('ONLINE_LMS_DB_HOST', 'ONLINE_LMS_DB_PORT', 'ONLINE_LMS_DB_NAME',
                'ONLINE_LMS_DB_USER', 'ONLINE_LMS_DB_PASSWORD',
                'WHAPI_TOKEN_PAID', 'WHATSAPP_GROUP_ADMISSION_LEDGER'):
        val = os.getenv(key)
        if val:
            masked = val[:6] + '...' + val[-4:] if len(val) > 12 else '***'
            print(f"   ✓  {key} = {masked}")
        else:
            print(f"   ✗  {key} = NOT SET")

    # ── DAILY ────────────────────────────────────────────────────────────
    # Window = [8:30 PM IST on the previous day, 8:30 PM IST on REPORT_DATE)
    daily_start = cutoff_instant(REPORT_DATE - timedelta(days=1))
    daily_end   = cutoff_instant(REPORT_DATE)
    print("\n─── Fetching: Daily admissions ────────────────────────────────")
    print(f"   Window: {daily_start} → {daily_end}  (UTC)")
    try:
        daily_rows = await fetch_admission_ledger(daily_start, daily_end)
    except Exception as e:
        import traceback
        print(f"❌  DB QUERY FAILED (daily): {e}")
        traceback.print_exc()
        sys.exit(1)
    daily_total = sum(r['total_admissions'] for r in daily_rows)
    print(f"  Rows: {len(daily_rows)} | Total admissions: {daily_total}")

    date_display = _fmt_date_display(REPORT_DATE)
    await build_and_send(
        daily_rows,
        title="The Daily Ledger",
        stamp_label=date_display.upper(),
        note=f"Window covers {date_display}, 8:30 PM cutoff — admissions marked after 8:30 PM roll into the next day's report.",
        page_title=f"Daily Ledger — {date_display}",
        filename_stub="Daily_Ledger",
        caption=f"📒 Daily Ledger — {date_display}\n{daily_total} total admissions",
    )

    # ── MTD ──────────────────────────────────────────────────────────────
    # Window = [8:30 PM IST on the day before month start, 8:30 PM IST on REPORT_DATE)
    mtd_start = cutoff_instant(MONTH_START - timedelta(days=1))
    mtd_end   = cutoff_instant(REPORT_DATE)
    print("\n─── Fetching: Month-to-date admissions ─────────────────────────")
    print(f"   Window: {mtd_start} → {mtd_end}  (UTC)")
    try:
        mtd_rows = await fetch_admission_ledger(mtd_start, mtd_end)
    except Exception as e:
        import traceback
        print(f"❌  DB QUERY FAILED (mtd): {e}")
        traceback.print_exc()
        sys.exit(1)
    mtd_total = sum(r['total_admissions'] for r in mtd_rows)
    print(f"  Rows: {len(mtd_rows)} | Total admissions: {mtd_total}")

    month_label = MONTH_START.strftime('%b %Y').upper()
    mtd_stamp = f"{month_label} · MTD (1–{REPORT_DATE.day})"
    mtd_note = (f"Window covers {MONTH_START.strftime(_day_fmt() + ' %b')}–{date_display}, "
                f"8:30 PM cutoff (month-to-date) — admissions marked after 8:30 PM roll into the next day.")
    await build_and_send(
        mtd_rows,
        title="The Monthly Ledger",
        stamp_label=mtd_stamp,
        note=mtd_note,
        page_title=f"Monthly Ledger — {MONTH_START.strftime('%B %Y')} (MTD)",
        filename_stub="Monthly_Ledger",
        caption=f"📒 Monthly Ledger — MTD ({MONTH_START.strftime(_day_fmt() + ' %b')}–{date_display})\n{mtd_total} total admissions",
    )

    # ── Cleanup ──────────────────────────────────────────────────────────
    print("\n─── Cleaning up output folder ───────────────────────────────────")
    if NO_CLEANUP or LOCAL_MODE:
        print(f"  ⏭️  Skipping cleanup ({'--nocleanup' if NO_CLEANUP else '--local'})")
        return
    removed = 0
    for item in os.listdir(OUTPUT_DIR):
        item_path = os.path.join(OUTPUT_DIR, item)
        try:
            if os.path.isfile(item_path):
                os.remove(item_path)
                removed += 1
        except Exception:
            pass
    print(f"  🧹 Removed {removed} file(s)")

    print("\n=== Done ===")


if __name__ == '__main__':
    asyncio.run(main())
