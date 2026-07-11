#!/usr/bin/env python3
"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CALLBACK REPORTS — Today's Callback Queue · Overdue Callback Alert
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Two live snapshots of the callback_date field on student_remarks (latest
remark per student), grouped by team (supervisor), for the online LOB.

  1. Today's Callback Queue    — callback_date = today (IST)
  2. Overdue Callback Alert    — callback_date < today (IST)

Counsellor roster + team (supervisor) mapping is read live from the same
Google Sheet used by generate_all_lms_reports.py — the "Counsellor Wise
Targets" tab (via sheets_config.load_counsellor_fee_targets()). Adding /
removing a counsellor there updates this report automatically, no code
changes needed.

DB      : Online LMS (same creds as tat_reports.py)
Delivery: PNGs via WHAPI -> WHATSAPP_GROUP_ONLINE_LOB
Cadence : every 3 hours, starting 6 AM IST (see server.py)

Usage:
  python generate_callback_reports.py            # generate + send
  python generate_callback_reports.py --local     # generate only, skip WhatsApp
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

sys.path.insert(0, BASE_DIR)
from sheets_config import load_counsellor_fee_targets, _norm

OUTPUT_DIR = os.path.join(BASE_DIR, 'Automation Cron Job', 'Callback Report')
os.makedirs(OUTPUT_DIR, exist_ok=True)

now_ist  = datetime.now(UTC) + timedelta(hours=5, minutes=30)
DATE_LABEL = now_ist.strftime('%#d %b %Y') if sys.platform == 'win32' else now_ist.strftime('%-d %b %Y')
RUN_STAMP  = now_ist.strftime('%Y-%m-%d_%H-%M')

# ── DB ───────────────────────────────────────────────────────────────────────
ONLINE_DB = {
    "host":     os.getenv("ONLINE_LMS_DB_HOST"),
    "port":     int(os.getenv("ONLINE_LMS_DB_PORT", "54321")),
    "database": os.getenv("ONLINE_LMS_DB_NAME"),
    "user":     os.getenv("ONLINE_LMS_DB_USER"),
    "password": os.getenv("ONLINE_LMS_DB_PASSWORD"),
}

# ── WhatsApp ─────────────────────────────────────────────────────────────────
WHAPI_TOKEN = os.getenv('WHAPI_TOKEN_PAID')
SEND_GROUPS = [g.strip() for g in os.getenv('WHATSAPP_GROUP_ONLINE_LOB', '120363424062745706@g.us').split(',') if g.strip()]

# ═══════════════════════════════════════════════════════════════════════════════
# SQL QUERIES  — counsellor_id list is resolved at runtime from the sheet roster
# ═══════════════════════════════════════════════════════════════════════════════

_LATEST_REMARK_CTE = """
WITH latest AS (
  SELECT DISTINCT ON (sr.student_id)
    sr.student_id,
    sr.callback_date
  FROM student_remarks sr
  WHERE sr.isdisabled = false
  ORDER BY sr.student_id, sr.created_at DESC
)
"""

TODAY_SQL = _LATEST_REMARK_CTE + """
SELECT
  l2.counsellor_name AS l2_name,
  l2.counsellor_id,
  COUNT(*) AS cnt
FROM latest lt
INNER JOIN students s     ON lt.student_id = s.student_id
INNER JOIN counsellors l2 ON s.assigned_counsellor_id = l2.counsellor_id
WHERE lt.callback_date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
  AND l2.counsellor_id = ANY($1::text[])
GROUP BY l2.counsellor_name, l2.counsellor_id
ORDER BY l2.counsellor_name;
"""

OVERDUE_SQL = _LATEST_REMARK_CTE + """
SELECT
  l2.counsellor_name AS l2_name,
  l2.counsellor_id,
  COUNT(*) AS cnt
FROM latest lt
INNER JOIN students s     ON lt.student_id = s.student_id
INNER JOIN counsellors l2 ON s.assigned_counsellor_id = l2.counsellor_id
WHERE lt.callback_date < (NOW() AT TIME ZONE 'Asia/Kolkata')::date
  AND l2.counsellor_id = ANY($1::text[])
GROUP BY l2.counsellor_name, l2.counsellor_id
ORDER BY l2.counsellor_name;
"""


def load_roster():
    """
    Roster + team (supervisor) mapping straight from the Google Sheet
    ("Counsellor Wise Targets" tab). Returns {counsellor_name_normalised: supervisor_name_normalised}.
    """
    _targets, sup_map = load_counsellor_fee_targets()
    return sup_map


async def fetch_all():
    sup_map = load_roster()
    print(f"  📋 Roster loaded from sheet: {len(sup_map)} counsellors, "
          f"{len(set(sup_map.values()))} teams")

    conn = await asyncpg.connect(**ONLINE_DB)
    try:
        db_counsellors = await conn.fetch("SELECT counsellor_id, counsellor_name FROM counsellors")
        id_by_name = {_norm(r['counsellor_name']): r['counsellor_id'] for r in db_counsellors}

        matched_ids = []
        unmatched = []
        for name in sup_map:
            cid = id_by_name.get(name)
            if cid:
                matched_ids.append(cid)
            else:
                unmatched.append(name)
        if unmatched:
            print(f"  ⚠️  {len(unmatched)} sheet counsellor(s) not found in DB by name: {unmatched}")

        today_rows   = await conn.fetch(TODAY_SQL,   matched_ids)
        overdue_rows = await conn.fetch(OVERDUE_SQL, matched_ids)
    finally:
        await conn.close()

    def attach_team(rows):
        out = []
        for r in rows:
            d = dict(r)
            d['to_name'] = sup_map.get(_norm(d['l2_name']), 'Unmapped')
            out.append(d)
        return out

    return attach_team(today_rows), attach_team(overdue_rows)


# ═══════════════════════════════════════════════════════════════════════════════
# AGGREGATION — group by team (to_name), split into two balanced columns
# ═══════════════════════════════════════════════════════════════════════════════

def e(v): return _html.escape(str(v))


def build_teams(rows):
    teams = {}
    for r in rows:
        teams.setdefault(r['to_name'], []).append({'name': r['l2_name'], 'count': int(r['cnt'])})
    for members in teams.values():
        members.sort(key=lambda m: -m['count'])
    return teams


def split_columns(teams):
    """Greedy bin-pack team blocks into two columns, balanced by total row count."""
    order = sorted(teams.keys(), key=lambda t: -sum(m['count'] for m in teams[t]))
    left, right = [], []
    left_h, right_h = 0, 0
    for t in order:
        h = len(teams[t]) + 1
        if left_h <= right_h:
            left.append(t); left_h += h
        else:
            right.append(t); right_h += h
    return left, right


def team_block_html(team_name, members):
    total = sum(m['count'] for m in members)
    rows_html = ''
    for i, m in enumerate(members, 1):
        zero = ' zero' if not m['count'] else ''
        rows_html += f'''
        <tr class="data-row{zero}">
          <td class="idx">{i}</td>
          <td>{e(m["name"])}</td>
          <td class="num count">{m["count"]}</td>
        </tr>'''
    return f'''
      <div class="team-block-wrap">
        <table class="mini">
          <thead>
            <tr class="team-row"><th colspan="2">{e(team_name)}</th><th class="num">{total}</th></tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>'''


def build_html(rows, *, title, kicker, subtitle, meta_word, accent, glow, footer_label):
    teams = build_teams(rows)
    left_teams, right_teams = split_columns(teams)
    left_html  = ''.join(team_block_html(t, teams[t]) for t in left_teams)
    right_html = ''.join(team_block_html(t, teams[t]) for t in right_teams)
    grand_total   = sum(m['count'] for members in teams.values() for m in members)
    n_counsellors = sum(len(members) for members in teams.values())

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{e(title)} — DegreeFYD Ops</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --navy: #0E1B2B;
    --paper: #FBF9F4;
    --ink: #1B1B18;
    --ink-soft: #7A7568;
    --accent: {accent};
    --glow: {glow};
    --line: #E5E0D4;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--navy);
    font-family: 'Inter', sans-serif;
    padding: 24px 14px 36px;
  }}
  .wrap {{
    max-width: 980px;
    margin: 0 auto;
    background: var(--paper);
    border-radius: 14px;
    overflow: hidden;
    box-shadow: 0 20px 60px rgba(0,0,0,0.35);
  }}
  header {{
    padding: 22px 26px 16px;
    border-bottom: 3px solid var(--accent);
    background: linear-gradient(135deg, {glow}12, transparent 60%);
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    flex-wrap: wrap;
    gap: 14px;
  }}
  .kicker {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: {glow};
    margin: 0 0 6px;
    font-weight: 600;
  }}
  h1 {{
    font-family: 'Space Grotesk', sans-serif;
    font-size: 23px;
    font-weight: 700;
    margin: 0 0 6px;
    color: var(--ink);
    letter-spacing: -0.01em;
  }}
  .subtitle {{
    font-size: 12px;
    color: var(--ink-soft);
    margin: 0;
    line-height: 1.5;
    max-width: 440px;
  }}
  .head-right {{ text-align: right; }}
  .date-chip {{
    background: var(--ink);
    color: var(--paper);
    padding: 5px 12px;
    border-radius: 4px;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    display: inline-block;
    margin-bottom: 8px;
  }}
  .meta-line {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    color: var(--ink-soft);
  }}

  .columns {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0;
    align-items: start;
  }}
  .col {{ padding: 18px 0 26px; }}
  .col.left {{ border-right: 1px dashed var(--line); padding-right: 0; }}

  .team-block-wrap {{
    padding: 0 22px 20px;
    margin-bottom: 20px;
    border-bottom: 2px solid var(--line);
  }}
  .team-block-wrap:last-child {{ margin-bottom: 0; border-bottom: none; padding-bottom: 0; }}
  table.mini {{
    width: 100%;
    border-collapse: collapse;
  }}

  tr.team-row th {{
    background: var(--ink);
    color: var(--paper);
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 600;
    font-size: 12px;
    padding: 8px 18px;
    text-align: left;
  }}
  tr.team-row th.num {{
    text-align: right;
    font-family: 'IBM Plex Mono', monospace;
    color: {accent};
    font-size: 13px;
  }}

  td, th {{ padding: 6px 18px; font-size: 12.5px; }}
  tr.data-row td {{
    color: var(--ink);
    border-bottom: 1px solid var(--line);
  }}
  tr.data-row td.idx {{
    color: var(--ink-soft);
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    width: 22px;
  }}
  tr.data-row td.num {{ text-align: right; }}
  tr.data-row td.count {{
    font-family: 'IBM Plex Mono', monospace;
    color: {glow};
    font-weight: 600;
    font-size: 13px;
  }}
  tr.data-row.zero td {{ color: #ADA795; }}
  tr.data-row.zero td.count {{ color: #C9C3B4; font-weight: 400; }}
  tr.data-row:last-child td {{ border-bottom: 2px solid var(--line); }}
  tr.data-row:hover td {{ background: {glow}0c; }}

  .grand-bar {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 16px 26px;
    background: {glow}14;
    border-top: 2px solid var(--accent);
  }}
  .grand-bar .label {{
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 700;
    font-size: 14px;
    color: var(--ink);
  }}
  .grand-bar .value {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 22px;
    font-weight: 600;
    color: {glow};
  }}

  footer.note {{
    padding: 10px 26px 18px;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 9.5px;
    color: var(--ink-soft);
    text-align: center;
    letter-spacing: 0.02em;
  }}

  @media (max-width: 620px) {{
    .columns {{ grid-template-columns: 1fr; }}
    .col.left {{ border-right: none; border-bottom: 1px dashed var(--line); }}
  }}
</style>
</head>
<body>
  <div class="wrap">
    <header>
      <div>
        <p class="kicker">{e(kicker)}</p>
        <h1>{e(title)}</h1>
        <p class="subtitle">{e(subtitle)}</p>
      </div>
      <div class="head-right">
        <div class="date-chip">{e(DATE_LABEL)} · IST</div>
        <div class="meta-line">{e(meta_word)} · {n_counsellors} counsellors</div>
      </div>
    </header>

    <div class="columns">
      <div class="col left">{left_html}</div>
      <div class="col right">{right_html}</div>
    </div>

    <div class="grand-bar">
      <span class="label">Grand Total ({e(footer_label)})</span>
      <span class="value">{grand_total:,}</span>
    </div>

    <footer class="note">DEGREEFYD OPS &middot; CALLBACK REPORTING &middot; latest remark per student · IST-filtered · assigned_counsellor_id</footer>
  </div>
</body>
</html>'''


# ═══════════════════════════════════════════════════════════════════════════════
# SCREENSHOT + WHAPI SEND
# ═══════════════════════════════════════════════════════════════════════════════

async def screenshot(html_path, png_path):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={'width': 1100, 'height': 900}, device_scale_factor=2)
        await page.goto(f'file:///{os.path.abspath(html_path)}', wait_until='networkidle', timeout=30000)
        await page.screenshot(path=png_path, full_page=True)
        await browser.close()


def send_via_whapi(file_path, caption, group_id):
    if not WHAPI_TOKEN or not group_id:
        print(f"  ⚠️  Missing WHAPI token or group — skipping {os.path.basename(file_path)}")
        return False
    import base64
    with open(file_path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode()
    filename = os.path.basename(file_path)
    payload = {'to': group_id, 'media': f'data:image/png;name={filename};base64,{b64}', 'caption': caption}
    headers = {'accept': 'application/json', 'authorization': f'Bearer {WHAPI_TOKEN}', 'content-type': 'application/json'}
    for attempt in range(2):
        try:
            r = requests.post('https://gate.whapi.cloud/messages/image', headers=headers, json=payload, timeout=20)
            if 200 <= r.status_code < 300:
                print(f"  ✅ Sent to {group_id[:12]}…: {filename}")
                return True
            print(f"  ⚠️  WHAPI HTTP {r.status_code}: {r.text[:120]}")
            return False
        except requests.exceptions.Timeout:
            print(f"  ⚠️  WHAPI timeout (attempt {attempt + 1})")
            if attempt == 0:
                time.sleep(3)
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

async def main():
    print("=" * 60)
    print("📞 CALLBACK REPORTS — Today's Queue · Overdue Alert")
    print("=" * 60)
    print(f"   As of: {DATE_LABEL} IST")

    today_rows, overdue_rows = await fetch_all()
    print(f"  ✅ Today's queue rows : {len(today_rows)}")
    print(f"  ✅ Overdue rows       : {len(overdue_rows)}")

    reports = [
        {
            'name': 'Todays_Callback_Queue',
            'caption': f"📅 Today's Callback Queue — {DATE_LABEL} IST",
            'html': build_html(
                today_rows,
                title="Today's Callback Queue",
                kicker='Scheduled · Due Today',
                subtitle='Callback due today per counsellor (IST), grouped by team owner.',
                meta_word='Scheduled for today',
                accent='#3FB6A3', glow='#0F6B5C',
                footer_label='Due Today',
            ),
        },
        {
            'name': 'Overdue_Callback_Alert',
            'caption': f"⏰ Overdue Callback Alert — {DATE_LABEL} IST",
            'html': build_html(
                overdue_rows,
                title='Overdue Callback Alert',
                kicker='Overdue · Callback Date Passed',
                subtitle='Callback date already passed per counsellor (IST), grouped by team owner.',
                meta_word='Overdue callbacks',
                accent='#E2572B', glow='#B33418',
                footer_label='Overdue',
            ),
        },
    ]

    for rep in reports:
        html_path = os.path.join(OUTPUT_DIR, f'{rep["name"]}_{RUN_STAMP}.html')
        png_path  = html_path.replace('.html', '.png')
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(rep['html'])
        print(f"  📄 HTML saved → {html_path}")

        await screenshot(html_path, png_path)
        print(f"  🖼️  Screenshot saved → {png_path}")

        if LOCAL_MODE:
            print(f"  [local] skipping WhatsApp send for {rep['name']}")
        else:
            for gid in SEND_GROUPS:
                send_via_whapi(png_path, rep['caption'], gid)
            for f_ in [html_path, png_path]:
                try:
                    if os.path.exists(f_):
                        os.remove(f_)
                except Exception as ex:
                    print(f"  ⚠️  Could not remove {f_}: {ex}")

    print("=" * 60)
    print("✅ CALLBACK REPORTS COMPLETE")
    print("=" * 60)


if __name__ == '__main__':
    asyncio.run(main())
