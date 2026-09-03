#!/usr/bin/env python3
"""
Servicing Call Report — pulls call stats straight from the LeadLens DB, builds HTML,
screenshots it, and sends via WhatsApp to an individual number (WHATSAPP_COMPETITOR_ADS_TO).

Usage:  python generate_servicing_report.py [--local] [--mode hourly|cumulative] [--date DD/MM/YYYY]
  --local       skip WhatsApp send, just save the HTML/PNG
  --mode        hourly     -> covers the last completed hour (e.g. run at 14:05 -> 13:00-14:00)
                cumulative -> covers 11:00 IST up to now, capped at 20:30 (default)
  --date        override report date (default: today IST)
  --from-time   HH:MM — explicit override for the window start
  --to-time     HH:MM — explicit override for the window end (exclusive)
  --to          override WhatsApp recipient (default: WHATSAPP_COMPETITOR_ADS_TO)
"""

import asyncio
import os
import sys
import argparse
import time
from datetime import datetime, timedelta, UTC, time as dtime
from dotenv import load_dotenv
from playwright.async_api import async_playwright
import asyncpg
import requests

_START = time.perf_counter()

def log(msg: str):
    elapsed = time.perf_counter() - _START
    print(f'[{elapsed:6.1f}s] {msg}', flush=True)

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# ─── Args ────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(add_help=False)
parser.add_argument('--local',      action='store_true')
parser.add_argument('--mode',       default='cumulative', choices=['hourly', 'cumulative'])
parser.add_argument('--date',       default=None, help='DD/MM/YYYY')
parser.add_argument('--from-time',  default=None, dest='from_time', help='HH:MM — override window start')
parser.add_argument('--to-time',    default=None, dest='to_time',   help='HH:MM — override window end (exclusive)')
parser.add_argument('--to',         default=None, help='Override WhatsApp recipient number')
args, _ = parser.parse_known_args()

LOCAL_MODE = args.local

# ─── Paths / env ─────────────────────────────────────────────────────────────
_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_DIR, '.env'))

OUTPUT_DIR = os.path.join(_DIR, 'Automation Cron Job', 'Servicing Report')
os.makedirs(OUTPUT_DIR, exist_ok=True)

WHAPI_TOKEN  = os.getenv('WHAPI_TOKEN_PAID')
WHATSAPP_TO  = args.to or os.getenv('WHATSAPP_COMPETITOR_ADS_TO')
LEADLENS_DSN = os.getenv('LEADLENS_DSN')

# ─── Report window boundaries ────────────────────────────────────────────────
WINDOW_START = '11:00'
WINDOW_END   = '20:30'

# ─── Counsellor → SIM → Role mapping ────────────────────────────────────────
COUNSELLOR_MAP = {
    '6357725430': ('Divya',   'Servicing'),
    '6357725436': ('Neetu',   'Servicing'),
    '6357725414': ('Anshika', 'Servicing'),
    '6357725440': ('Manoj',   'Documentation'),
}
ROLE_ORDER  = ['Servicing', 'Documentation']
ROLE_COLORS = {
    'Servicing':     '#0ea5e9',
    'Documentation': '#8b5cf6',
}

# ─── Date / time-window logic ────────────────────────────────────────────────
if args.date:
    report_dt = datetime.strptime(args.date, '%d/%m/%Y')
else:
    report_dt = datetime.now(UTC) + timedelta(hours=5, minutes=30)

report_date_str = report_dt.strftime('%Y-%m-%d')
date_label = report_dt.strftime('%#d %B %Y') if sys.platform == 'win32' else report_dt.strftime('%-d %B %Y')

now_ist = datetime.now(UTC) + timedelta(hours=5, minutes=30)

if args.from_time or args.to_time:
    from_time = args.from_time or WINDOW_START
    to_time   = args.to_time   or now_ist.strftime('%H:%M')
elif args.mode == 'hourly':
    hour_end   = now_ist.replace(minute=0, second=0, microsecond=0)
    hour_start = hour_end - timedelta(hours=1)
    from_time  = max(hour_start.strftime('%H:%M'), WINDOW_START)
    to_time    = hour_end.strftime('%H:%M')
    if to_time <= from_time:
        from_time, to_time = WINDOW_START, WINDOW_START  # degenerate, will yield empty report
else:  # cumulative
    from_time = WINDOW_START
    to_time   = min(now_ist.strftime('%H:%M'), WINDOW_END)

RUN_STAMP = report_dt.strftime('%d%m%Y') + '_' + now_ist.strftime('%H%M')


# ─── DB query ────────────────────────────────────────────────────────────────

async def fetch_calls(target_date: str, from_time: str, to_time: str):
    log(f'Connecting to LeadLens DB ...')
    conn = await asyncpg.connect(dsn=LEADLENS_DSN, timeout=20)
    try:
        date_obj = datetime.strptime(target_date, '%Y-%m-%d').date()
        from_t   = dtime.fromisoformat(from_time)
        to_t     = dtime.fromisoformat(to_time)
        rows = await conn.fetch(
            '''
            SELECT sim_number, direction, status, duration, ring_duration
            FROM leadlens_calls
            WHERE call_date = $1
              AND sim_number = ANY($2::text[])
              AND call_time >= $3
              AND call_time <  $4
            ''',
            date_obj, list(COUNSELLOR_MAP.keys()), from_t, to_t,
        )
        log(f'Fetched {len(rows)} call rows')
        return rows
    finally:
        await conn.close()


def hms_to_secs(v: str) -> int:
    if not v:
        return 0
    parts = str(v).split(':')
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        pass
    return 0


def process_rows(rows):
    stats = {}
    for sim, (name, role) in COUNSELLOR_MAP.items():
        stats[name] = {
            'role': role, 'total': 0, 'inbound': 0, 'outbound': 0,
            'answered': 0, 'talk_secs': 0, 'ring_secs': 0, 'ring_calls': 0,
        }

    for row in rows:
        sim = row['sim_number']
        if sim not in COUNSELLOR_MAP:
            continue
        name = COUNSELLOR_MAP[sim][0]
        s = stats[name]
        s['total'] += 1
        if (row['direction'] or '').strip().upper() == 'INBOUND':
            s['inbound'] += 1
        else:
            s['outbound'] += 1

        ring_secs = hms_to_secs(row['ring_duration'])
        if ring_secs:
            s['ring_secs']  += ring_secs
            s['ring_calls'] += 1

        if (row['status'] or '').strip() == 'Answered':
            s['answered']  += 1
            s['talk_secs'] += hms_to_secs(row['duration'])

    for s in stats.values():
        s['connect_pct'] = round(s['answered'] / s['total'] * 100, 1) if s['total'] else 0.0
        s['avg_secs']    = round(s['talk_secs'] / s['answered'])      if s['answered']   else 0
        s['avg_ring']    = round(s['ring_secs'] / s['ring_calls'])    if s['ring_calls'] else 0

    return stats


# ─── HTML generation ─────────────────────────────────────────────────────────

def fmt_talk(secs: int) -> str:
    if not secs:
        return '—'
    if secs < 60:
        return f'{secs}s'
    m = round(secs / 60)
    h, mn = divmod(m, 60)
    return f'{h}h {mn:02d}m' if h else f'{m}m'


def fmt_avg(secs: int) -> str:
    if not secs:
        return '—'
    m, s = divmod(secs, 60)
    return f'{m}m {s:02d}s' if m else f'{s}s'


def build_html(stats: dict, date_label: str, time_window: str, mode_label: str) -> str:
    all_c   = list(stats.values())
    g_total = sum(c['total']     for c in all_c)
    g_in    = sum(c['inbound']   for c in all_c)
    g_out   = sum(c['outbound']  for c in all_c)
    g_ans   = sum(c['answered']  for c in all_c)
    g_talk  = sum(c['talk_secs'] for c in all_c)
    g_ring  = sum(c['ring_secs'] for c in all_c)
    g_ringn = sum(c['ring_calls'] for c in all_c)
    g_rate  = round(g_ans / g_total * 100) if g_total else 0
    g_avg   = round(g_talk / g_ans)        if g_ans   else 0
    g_avg_ring = round(g_ring / g_ringn)   if g_ringn else 0
    g_overall = g_ring + g_talk
    n_active = len([c for c in all_c if c['total'] > 0])

    ordered = sorted(
        stats.items(),
        key=lambda kv: (ROLE_ORDER.index(kv[1]['role']) if kv[1]['role'] in ROLE_ORDER else 99, -kv[1]['talk_secs']),
    )

    rows = ''
    for name, s in ordered:
        color = ROLE_COLORS.get(s['role'], '#6b7280')
        zero  = 'zero-row' if not s['total'] else ''
        rows += f'''<tr class="{zero}">
      <td><span class="rdot" style="background:{color}"></span>{name}<span class="rlabel" style="color:{color}">{s["role"]}</span></td>
      <td>{s["total"] or "0"}</td>
      <td>{s["inbound"] or "0"}</td>
      <td>{s["outbound"] or "0"}</td>
      <td>{fmt_avg(s["avg_ring"])}</td>
      <td>{fmt_talk(s["ring_secs"])}</td>
      <td>{fmt_talk(s["talk_secs"])}</td>
      <td>{fmt_talk(s["ring_secs"] + s["talk_secs"])}</td>
      <td>{fmt_avg(s["avg_secs"])}</td>
    </tr>'''

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Servicing Report · {date_label}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Inter', sans-serif;
    background: #F8F9FB;
    color: #111827;
    padding: 36px 40px 48px;
    font-size: 15px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }}
  .topbar {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 28px; }}
  .topbar-left {{ display: flex; align-items: center; gap: 14px; }}
  .logo-mark {{
    width: 40px; height: 40px; border-radius: 10px;
    background: #0f766e; display: flex; align-items: center; justify-content: center;
    font-size: 13px; font-weight: 800; color: #fff; letter-spacing: -0.5px;
  }}
  .topbar h1 {{ font-size: 1.15rem; font-weight: 700; color: #111827; }}
  .topbar p  {{ font-size: .85rem; color: #4b5563; margin-top: 2px; }}
  .pill {{
    font-size: .72rem; font-weight: 700; letter-spacing: .8px; text-transform: uppercase;
    padding: 6px 14px; border-radius: 999px;
    background: #f0fdfa; color: #0f766e; border: 1px solid #99f6e4;
  }}
  .kpis {{ display: grid; grid-template-columns: repeat(7,1fr); gap: 10px; margin-bottom: 24px; }}
  .kpi-card {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 16px 18px; position: relative; overflow: hidden; }}
  .kpi-card::after {{ content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px; background: var(--ac, #e5e7eb); border-radius: 12px 12px 0 0; }}
  .kpi-card .v {{ font-size: 1.65rem; font-weight: 800; color: var(--vc, #111827); line-height: 1; margin-top: 4px; }}
  .kpi-card .l {{ font-size: .7rem; font-weight: 600; color: #4b5563; text-transform: uppercase; letter-spacing: .6px; margin-top: 8px; }}
  .tcard {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 14px; overflow: hidden; }}
  table {{ width: 100%; border-collapse: collapse; }}
  thead th {{
    padding: 10px 16px; font-size: .72rem; font-weight: 700; text-transform: uppercase; letter-spacing: .55px;
    color: #4b5563; text-align: right; white-space: nowrap; background: #F5F6F8; border-bottom: 1px solid #e5e7eb;
  }}
  thead th:first-child {{ text-align: left; }}
  tbody td {{ padding: 12px 16px; text-align: right; color: #121212; font-size: .88rem; font-weight: 700; border-bottom: 1px solid #f3f4f6; font-variant-numeric: tabular-nums; }}
  tbody td:first-child {{ text-align: left; font-weight: 700; color: #121212; display: flex; align-items: center; gap: 8px; }}
  tbody tr:last-child td {{ border-bottom: none; }}
  tbody tr:hover td {{ background: #f9fafb; }}
  .zero-row td {{ color: #9ca3af; }}
  .zero-row td:first-child {{ color: #6b7280; font-weight: 500; }}
  .rdot {{ width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; display: inline-block; }}
  .rlabel {{ font-size: .68rem; font-weight: 700; text-transform: uppercase; letter-spacing: .5px; margin-left: 4px; }}
  footer {{ text-align: center; margin-top: 24px; font-size: .75rem; color: #6b7280; }}
</style>
</head>
<body>

<div class="topbar">
  <div class="topbar-left">
    <div class="logo-mark">SV</div>
    <div>
      <h1>Servicing Call Report</h1>
      <p>{date_label} &nbsp;·&nbsp; {time_window} &nbsp;·&nbsp; {mode_label} &nbsp;·&nbsp; {n_active} active</p>
    </div>
  </div>
  <span class="pill">Servicing</span>
</div>

<div class="kpis">
  <div class="kpi-card" style="--ac:#e5e7eb"><div class="v">{g_total}</div><div class="l">Total calls</div></div>
  <div class="kpi-card" style="--ac:#e5e7eb"><div class="v">{g_in} / {g_out}</div><div class="l">Inbound / Outbound</div></div>
  <div class="kpi-card" style="--ac:#10b981;--vc:#0f766e"><div class="v">{g_ans}</div><div class="l">Picked up</div></div>
  <div class="kpi-card" style="--ac:#3b82f6;--vc:#2563eb"><div class="v">{g_rate}%</div><div class="l">Pick-up rate</div></div>
  <div class="kpi-card" style="--ac:#e5e7eb"><div class="v">{fmt_talk(g_talk)}</div><div class="l">Total talk time</div></div>
  <div class="kpi-card" style="--ac:#f59e0b;--vc:#b45309"><div class="v">{fmt_avg(g_avg_ring)}</div><div class="l">Avg ring time</div></div>
  <div class="kpi-card" style="--ac:#8b5cf6;--vc:#6d28d9"><div class="v">{fmt_talk(g_overall)}</div><div class="l">Overall talk time</div></div>
</div>

<div class="tcard">
  <table>
    <thead><tr>
      <th>Counsellor</th><th>Calls</th><th>Inbound</th><th>Outbound</th>
      <th>Avg&nbsp;Ring</th><th>Total&nbsp;Ring</th><th>Talk&nbsp;Time</th><th>Overall&nbsp;Talk&nbsp;Time</th><th>Avg&nbsp;/&nbsp;Call</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>

<footer>Source: LeadLens DB &nbsp;·&nbsp; {date_label} &nbsp;·&nbsp; {time_window}</footer>
</body>
</html>'''


# ─── Screenshot ──────────────────────────────────────────────────────────────

async def take_screenshot(html_path: str, png_path: str):
    log('Screenshot browser launching ...')
    async with async_playwright() as p:
        browser  = await p.chromium.launch(headless=True)
        page     = await browser.new_page(viewport={'width': 1200, 'height': 800}, device_scale_factor=2)
        file_url = 'file:///' + html_path.replace('\\', '/')
        await page.goto(file_url, wait_until='domcontentloaded')
        await page.wait_for_timeout(1200)
        full_height = await page.evaluate('document.body.scrollHeight')
        await page.set_viewport_size({'width': 1200, 'height': full_height})
        await page.wait_for_timeout(300)
        await page.screenshot(path=png_path, full_page=True)
        log(f'Screenshot saved → {png_path}')
        await browser.close()


# ─── WhatsApp send ────────────────────────────────────────────────────────────

def send_whatsapp_image(img_path: str, caption: str, to: str, token: str):
    import base64
    filename = os.path.basename(img_path)
    with open(img_path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode('utf-8')
    media_data = f'data:image/png;name={filename};base64,{b64}'
    payload = {'to': to, 'media': media_data, 'caption': caption}
    headers = {'accept': 'application/json', 'authorization': f'Bearer {token}', 'content-type': 'application/json'}
    for attempt in range(2):
        try:
            resp = requests.post('https://gate.whapi.cloud/messages/image', headers=headers, json=payload, timeout=20)
            if 200 <= resp.status_code < 300:
                print(f'WhatsApp image sent → {to}: {resp.status_code}')
                return resp.json()
            print(f'WHAPI error {resp.status_code}: {resp.text[:150]}')
            resp.raise_for_status()
        except Exception as e:
            print(f'WHAPI attempt {attempt + 1} failed: {e}')
            if attempt == 0:
                time.sleep(3)
    raise RuntimeError(f'WHAPI failed after 2 attempts for {filename}')


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    log('=== Servicing Report — START ===')
    if not LEADLENS_DSN:
        raise RuntimeError('LEADLENS_DSN env var is not set')

    mode_label  = 'Hourly' if args.mode == 'hourly' and not (args.from_time or args.to_time) else 'Cumulative'
    time_window = f'{from_time} – {to_time}'
    log(f'Date: {report_date_str}  Window: {time_window}  Mode: {mode_label}')

    rows  = await fetch_calls(report_date_str, from_time, to_time)
    stats = process_rows(rows)

    total_calls = sum(s['total']    for s in stats.values())
    total_ans   = sum(s['answered'] for s in stats.values())
    log(f'Parsed: {total_calls} calls, {total_ans} picked up')

    log('Building HTML report ...')
    html_content  = build_html(stats, date_label, time_window, mode_label)
    html_filename = f'Servicing_Report_{RUN_STAMP}_{mode_label}.html'
    html_path     = os.path.join(OUTPUT_DIR, html_filename)
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    log(f'HTML saved → {html_path}')

    png_path = html_path.replace('.html', '.png')
    await take_screenshot(html_path, png_path)

    if LOCAL_MODE:
        log('--local mode: skipping WhatsApp send')
        log('=== Done (local mode) ===')
        return

    if not WHATSAPP_TO:
        raise RuntimeError('WHATSAPP_COMPETITOR_ADS_TO env var is not set (and no --to given)')

    caption = (
        f'📲 Servicing Report — {mode_label}'
        f'\n📅 {date_label}'
        f'\n⏱ {time_window}'
        f'\n✅ {total_ans}/{total_calls} picked up'
    )
    log(f'Sending report to WhatsApp ({WHATSAPP_TO}) ...')
    try:
        send_whatsapp_image(png_path, caption, WHATSAPP_TO, WHAPI_TOKEN)
    except Exception as e:
        log(f'  ⚠️  Send failed: {e}')

    log('Cleaning up local files ...')
    for f in [html_path, png_path]:
        try:
            if os.path.exists(f):
                os.remove(f)
        except Exception as e:
            log(f'  ⚠️  Could not remove {f}: {e}')
    log('=== Done ===')


if __name__ == '__main__':
    asyncio.run(main())
