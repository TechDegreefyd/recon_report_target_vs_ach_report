#!/usr/bin/env python3
"""
Outbound Call Report — Download CSV from CallInsight, build HTML, send via WhatsApp.
Usage:  python generate_outbound_report.py [--local] [--date DD/MM/YYYY]
  --local   skip WhatsApp send, just save the HTML
  --date    override report date (default: today IST)
"""

import asyncio
import os
import sys
import re
import csv
import io
import argparse
import time
from datetime import datetime, timedelta, UTC
from dotenv import load_dotenv
from playwright.async_api import async_playwright
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
parser.add_argument('--date',       default=None,  help='DD/MM/YYYY')
parser.add_argument('--csv',        default=None,  help='Path to existing CSV (skips download)')
parser.add_argument('--from-time',  default=None,  dest='from_time', help='HH:MM — filter calls from this time')
parser.add_argument('--to-time',    default=None,  dest='to_time',   help='HH:MM — filter calls up to this time (exclusive)')
parser.add_argument('--group',      default=None,  help='Override WhatsApp group ID')
args, _ = parser.parse_known_args()

LOCAL_MODE = args.local

# ─── Paths / env ─────────────────────────────────────────────────────────────
_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_DIR, '.env'))

OUTPUT_DIR = os.path.join(_DIR, 'Automation Cron Job', 'Outbound Report')
os.makedirs(OUTPUT_DIR, exist_ok=True)

WHAPI_TOKEN = os.getenv('WHAPI_TOKEN')
WHATSAPP_GROUP = os.getenv('WHATSAPP_GROUP', '120363426619711887@g.us')

# ─── Date logic ──────────────────────────────────────────────────────────────
if args.date:
    report_date_str = args.date   # DD/MM/YYYY
    report_dt = datetime.strptime(args.date, '%d/%m/%Y')
else:
    now_ist = datetime.now(UTC) + timedelta(hours=5, minutes=30)
    report_dt = now_ist
    report_date_str = report_dt.strftime('%d/%m/%Y')

date_label = report_dt.strftime('%#d %B %Y') if sys.platform == 'win32' else report_dt.strftime('%-d %B %Y')
RUN_STAMP  = report_dt.strftime('%d%m%Y_%H%M')

# ─── Counsellor → SIM → Team mapping ────────────────────────────────────────
SIM_MAP = {
    '6357928504': ('Vishwajeet',   'Vishal Team'),
    '6357928508': ('Arnav',        'Vishal Team'),
    '6357928515': ('Avneet',       'Sunil Team'),   # Avneet is Sunil Team
    '6357928503': ('Himanshi',     'Sunil Team'),
    '6357928509': ('Vikas',        'Sunil Team'),
    '6357928511': ('Abhishek',     'Sunil Team'),
    '6357928505': ('Preeti',        'Sunil Team'),
    '6357928506': ('Prashant',     'Varun Team'),
    '6357928502': ('Anshika',      'Varun Team'),
    '6357928513': ('Tanisha',      'Varun Team'),
    '6357928518': ('Virat',        'Sid Team'),
    '6357928517': ('LaxmiNarayan', 'Sid Team'),
    '6357928512': ('Sanjay',       'Vartika Team'),
    '6357928501': ('Shiv',         'Vartika Team'),
    '6357928516': ('Sagar',        'Vartika Team'),
    '6357928519': ('Nitin',        'Vartika Team'),
    '6357928514': ('Aditya',       'Vartika Team'),
    '6357928510': ('Swapnil',      'Prashant Team'),
    '6357928507': ('Divya',        'Prashant Team'),
}

TEAM_ORDER = ['Vartika Team', 'Sunil Team', 'Vishal Team', 'Sid Team', 'Varun Team', 'Prashant Team']
TEAM_COLORS = {
    'Vartika Team':  '#8b5cf6',
    'Sunil Team':    '#0ea5e9',
    'Vishal Team':   '#2563eb',
    'Sid Team':      '#f59e0b',
    'Varun Team':    '#10b981',
    'Prashant Team': '#ec4899',
}

# ─── CallInsight download ─────────────────────────────────────────────────────
CALLINSIGHT_URL  = 'https://app.callinsight.io'
CALL_LOGS_URL    = 'https://app.callinsight.io/call-logs'
CI_EMAIL         = (os.getenv('CALLINSIGHT_EMAIL') or '').strip()
CI_PASSWORD      = (os.getenv('CALLINSIGHT_PASSWORD') or '').strip()
DOWNLOAD_DIR_CI  = os.path.join(_DIR, 'callinsight_downloads')


async def download_csv_playwright() -> str:
    if not CI_EMAIL or not CI_PASSWORD:
        raise RuntimeError('CALLINSIGHT_EMAIL or CALLINSIGHT_PASSWORD env var is not set')
    os.makedirs(DOWNLOAD_DIR_CI, exist_ok=True)

    log('Launching headless browser ...')
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
            ],
        )
        context = await browser.new_context(
            accept_downloads=True,
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/125.0.0.0 Safari/537.36'
            ),
        )
        page = await context.new_page()
        # hide webdriver flag
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        log('Browser ready')

        log('Loading CallInsight login page ...')
        await page.goto(CALLINSIGHT_URL, timeout=120_000, wait_until='domcontentloaded')
        await page.wait_for_timeout(3000)
        log(f'Login page loaded — URL: {page.url}')

        log(f'Filling credentials for {CI_EMAIL!r} ...')
        await page.click('input[name="email"]')
        await page.type('input[name="email"]',    CI_EMAIL,    delay=60)
        await page.click('input[type="password"]')
        await page.type('input[type="password"]', CI_PASSWORD, delay=60)
        await page.wait_for_timeout(500)
        await page.click('button[type="submit"]')
        log('Submitted login form — waiting for redirect ...')

        try:
            await page.wait_for_url(lambda url: 'login' not in url, timeout=120_000)
        except Exception:
            body_text = (await page.inner_text('body'))[:500].replace('\n', ' ')
            log(f'LOGIN FAILED — still on: {page.url}')
            log(f'Page body snippet: {body_text}')
            fail_shot = os.path.join(DOWNLOAD_DIR_CI, 'login_failure.png')
            await page.screenshot(path=fail_shot)
            log(f'Failure screenshot saved → {fail_shot}')
            raise
        await page.wait_for_load_state('domcontentloaded')
        log(f'Logged in — URL: {page.url}')

        log('Navigating to /call-logs ...')
        await page.goto(CALL_LOGS_URL, timeout=120_000, wait_until='domcontentloaded')
        await page.wait_for_timeout(4000)
        log(f'Call logs page ready — URL: {page.url}')

        log('Clicking Download CSV button ...')
        async with page.expect_download(timeout=120_000) as dl_info:
            await page.click('button:has-text("Download CSV")')
        dl = await dl_info.value
        save_path = os.path.join(DOWNLOAD_DIR_CI, dl.suggested_filename or 'call_logs.csv')
        await dl.save_as(save_path)
        log(f'CSV saved → {save_path}')

        await browser.close()
        log('Browser closed')

        with open(save_path, encoding='utf-8-sig', errors='replace') as f:
            return f.read()


# ─── CSV parsing ─────────────────────────────────────────────────────────────

def parse_date_cell(v: str) -> str:
    """Strip ="DD/MM/YYYY" wrapper → DD/MM/YYYY."""
    v = v.strip().strip('"')
    m = re.match(r'^=?"?([0-9]{2}/[0-9]{2}/[0-9]{4})"?$', v)
    return m.group(1) if m else v


def duration_to_secs(v: str) -> int:
    """HH:MM:SS → total seconds."""
    parts = str(v).strip().split(':')
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        pass
    return 0


def extract_sim(sim_field: str) -> str:
    """'6357928504 - Vishwajeet - Admin' → '6357928504'."""
    return sim_field.strip().split()[0].strip()


def process_csv(csv_text: str, target_date: str, from_time: str = None, to_time: str = None):
    """
    Filter OUTBOUND + target_date (+ optional time window) → per-counsellor stats.
    from_time / to_time: 'HH:MM' strings; window is [from_time, to_time).
    """
    stats = {}
    for sim, (name, team) in SIM_MAP.items():
        stats[name] = {'team': team, 'total': 0, 'answered': 0, 'talk_secs': 0}

    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        date_val = parse_date_cell(row.get('Date', ''))
        if date_val != target_date:
            continue
        if row.get('Call Type', '').strip().upper() != 'OUTBOUND':
            continue

        row_time = row.get('Time', '').strip()[:5]  # HH:MM
        if from_time and row_time < from_time:
            continue
        if to_time and row_time >= to_time:
            continue

        sim_raw  = row.get('SIM Number', '')
        sim_no   = extract_sim(sim_raw)
        if sim_no not in SIM_MAP:
            continue

        name     = SIM_MAP[sim_no][0]
        status   = row.get('Call Status', '').strip()
        dur_secs = duration_to_secs(row.get('Call Duration', '0:0:0'))

        stats[name]['total'] += 1
        if status == 'Answered':
            stats[name]['answered'] += 1
        stats[name]['talk_secs'] += dur_secs

    for name, s in stats.items():
        s['connect_pct'] = round(s['answered'] / s['total'] * 100, 1) if s['total'] else 0.0
        s['avg_secs']    = round(s['talk_secs'] / s['answered']) if s['answered'] else 0

    return stats


# ─── HTML generation ─────────────────────────────────────────────────────────

def fmt_talk(secs: int) -> str:
    if not secs:
        return '—'
    m = round(secs / 60)
    h, mn = divmod(m, 60)
    return f'{h}h {mn:02d}m' if h else f'{m}m'


def fmt_avg(secs: int) -> str:
    if not secs:
        return '—'
    m, s = divmod(secs, 60)
    return f'{m}m {s:02d}s' if m else f'{s}s'


def build_html(stats: dict, date_label: str, csv_source: str = '', time_window: str = '') -> str:
    # Global totals
    all_c    = list(stats.values())
    g_total        = sum(c['total']     for c in all_c)
    g_ans          = sum(c['answered']  for c in all_c)
    g_talk         = sum(c['talk_secs'] for c in all_c)
    g_rate         = round(g_ans / g_total * 100) if g_total else 0
    g_avg          = round(g_talk / g_ans)         if g_ans   else 0
    active_5plus   = sum(1 for c in all_c if c['total'] > 5)
    g_talk_per_act = round(g_talk / active_5plus)  if active_5plus else 0

    # Group by team
    teams = {}
    for name, s in stats.items():
        t = s['team']
        teams.setdefault(t, []).append({'name': name, **s})

    def chip(pct, total):
        if not total:
            return '<span class="chip cx">—</span>'
        cls = 'cg' if pct >= 45 else ('co' if pct >= 30 else 'cr')
        return f'<span class="chip {cls}">{pct}%</span>'

    left_teams  = [t for t in TEAM_ORDER if t in teams and TEAM_ORDER.index(t) < 3]
    right_teams = [t for t in TEAM_ORDER if t in teams and TEAM_ORDER.index(t) >= 3]

    def team_card(team_name):
        members = sorted(teams.get(team_name, []), key=lambda x: -x['talk_secs'])
        color   = TEAM_COLORS.get(team_name, '#6b7280')
        tc      = sum(m['total']     for m in members)
        ta      = sum(m['answered']  for m in members)
        tt      = sum(m['talk_secs'] for m in members)
        tr_     = round(ta / tc * 100) if tc else 0
        t_avg   = round(tt / ta)       if ta else 0

        rows = ''
        for m in members:
            zero = 'zero-row' if not m['total'] else ''
            rows += f'''<tr class="{zero}">
      <td>{m["name"]}</td>
      <td>{m["total"] or "0"}</td>
      <td>{m["answered"] or "0"}</td>
      <td>{chip(int(m["connect_pct"]), m["total"])}</td>
      <td>{fmt_talk(m["talk_secs"])}</td>
      <td>{fmt_avg(m["avg_secs"])}</td>
    </tr>'''

        return f'''<div class="tcard">
  <div class="tcard-head">
    <div class="tname" style="color:{color}">
      <span class="tdot" style="background:{color}"></span>{team_name}
    </div>
    <div class="tmeta">
      <span>{tc} calls</span>
      <span>{ta} connected</span>
      <span>{fmt_talk(tt)}</span>
    </div>
  </div>
  <table>
    <thead><tr>
      <th>Counsellor</th><th>Calls</th><th>Connected</th>
      <th>Connect&nbsp;%</th><th>Talk&nbsp;Time</th><th>Avg&nbsp;/&nbsp;Call</th>
    </tr></thead>
    <tbody>{rows}</tbody>
    <tfoot><tr>
      <td>Team Total</td>
      <td>{tc}</td><td>{ta}</td>
      <td>{chip(tr_, tc)}</td>
      <td>{fmt_talk(tt)}</td>
      <td>{fmt_avg(t_avg)}</td>
    </tr></tfoot>
  </table>
</div>'''

    left_html  = '\n'.join(team_card(t) for t in left_teams)
    right_html = '\n'.join(team_card(t) for t in right_teams)
    source_note   = csv_source or 'CallInsight'
    n_counsellors = len([c for c in all_c if c['total'] > 0])

    # ── Supervisor summary table ──────────────────────────────────────────────
    sup_rows_html = ''
    gt_tc = gt_ta = gt_tt = 0
    for team_name in TEAM_ORDER:
        members = teams.get(team_name, [])
        if not members:
            continue
        tc   = sum(m['total']     for m in members)
        ta   = sum(m['answered']  for m in members)
        tt   = sum(m['talk_secs'] for m in members)
        rate = round(ta / tc * 100) if tc else 0
        avg  = round(tt / ta)       if ta else 0
        a5   = sum(1 for m in members if m['total'] > 5)
        tpa  = round(tt / a5) if a5 else 0
        color = TEAM_COLORS.get(team_name, '#6b7280')
        gt_tc += tc; gt_ta += ta; gt_tt += tt
        sup_rows_html += f'''<tr>
      <td class="sup-name-cell" style="border-left:3px solid {color};">{team_name}</td>
      <td>{len(members)}</td><td>{tc}</td><td>{ta}</td>
      <td>{chip(rate, tc)}</td>
      <td>{fmt_talk(tt)}</td>
      <td>{fmt_avg(avg)}</td>
      <td>{fmt_talk(tpa)}</td>
    </tr>'''
    gt_rate = round(gt_ta / gt_tc * 100) if gt_tc else 0
    gt_avg  = round(gt_tt / gt_ta)       if gt_ta else 0
    gt_a5   = sum(1 for c in all_c if c['total'] > 5)
    gt_tpa  = round(gt_tt / gt_a5) if gt_a5 else 0
    sup_rows_html += f'''<tr class="sup-total">
      <td>Total</td><td>{n_counsellors}</td><td>{gt_tc}</td><td>{gt_ta}</td>
      <td>{chip(gt_rate, gt_tc)}</td>
      <td>{fmt_talk(gt_tt)}</td>
      <td>{fmt_avg(gt_avg)}</td>
      <td>{fmt_talk(gt_tpa)}<span class="act-badge">{gt_a5}</span></td>
    </tr>'''

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Outbound Report · {date_label}</title>
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
  .topbar {{
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 28px;
  }}
  .topbar-left {{ display: flex; align-items: center; gap: 14px; }}
  .logo-mark {{
    width: 40px; height: 40px; border-radius: 10px;
    background: #111827;
    display: flex; align-items: center; justify-content: center;
    font-size: 14px; font-weight: 800; color: #fff; letter-spacing: -0.5px;
  }}
  .topbar h1 {{ font-size: 1.15rem; font-weight: 700; color: #111827; }}
  .topbar p  {{ font-size: .85rem; color: #4b5563; margin-top: 2px; }}
  .pill {{
    font-size: .72rem; font-weight: 700; letter-spacing: .8px; text-transform: uppercase;
    padding: 6px 14px; border-radius: 999px;
    background: #f0fdf4; color: #16a34a; border: 1px solid #bbf7d0;
  }}
  .kpis {{ display: grid; grid-template-columns: repeat(6,1fr); gap: 10px; margin-bottom: 28px; }}
  .kpi-card {{
    background: #fff; border: 1px solid #e5e7eb;
    border-radius: 12px; padding: 16px 18px;
    position: relative; overflow: hidden;
  }}
  .kpi-card::after {{
    content: ''; position: absolute; top: 0; left: 0; right: 0;
    height: 3px; background: var(--ac, #e5e7eb); border-radius: 12px 12px 0 0;
  }}
  .kpi-card .v {{ font-size: 1.75rem; font-weight: 800; color: var(--vc, #111827); line-height: 1; margin-top: 4px; }}
  .kpi-card .l {{ font-size: .72rem; font-weight: 600; color: #4b5563; text-transform: uppercase; letter-spacing: .6px; margin-top: 8px; }}
  .layout {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; align-items: start; }}
  .col    {{ display: flex; flex-direction: column; gap: 16px; }}
  .tcard  {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 14px; overflow: hidden; }}
  .tcard-head {{
    padding: 14px 18px 13px; border-bottom: 1px solid #f3f4f6;
    display: flex; align-items: center; justify-content: space-between;
  }}
  .tname {{
    display: flex; align-items: center; gap: 9px;
    font-size: .82rem; font-weight: 700; text-transform: uppercase; letter-spacing: .7px;
  }}
  .tdot {{ width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }}
  .tmeta {{ font-size: .8rem; color: #4b5563; display: flex; gap: 4px; }}
  .tmeta span + span::before {{ content: '·'; margin-right: 4px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  thead th {{
    padding: 9px 16px;
    font-size: .72rem; font-weight: 700; text-transform: uppercase; letter-spacing: .55px;
    color: #4b5563; text-align: right; white-space: nowrap;
    background: #F5F6F8; border-bottom: 1px solid #e5e7eb;
  }}
  thead th:first-child {{ text-align: left; }}
  tbody td {{
    padding: 11px 16px; text-align: right;
    color: #121212; font-size: .88rem; font-weight: 700;
    border-bottom: 1px solid #f3f4f6;
    font-variant-numeric: tabular-nums;
  }}
  tbody td:first-child {{ text-align: left; font-weight: 700; color: #121212; }}
  tbody tr:last-child td {{ border-bottom: none; }}
  tbody tr:hover td {{ background: #f9fafb; }}
  .zero-row td {{ color: #9ca3af; }}
  .zero-row td:first-child {{ color: #6b7280; font-weight: 500; }}
  tfoot tr {{ background: #f5f6f8; }}
  tfoot td {{
    padding: 11px 16px; text-align: right;
    font-size: .82rem; font-weight: 700; color: #1f2937;
    border-top: 2px solid #e5e7eb;
    font-variant-numeric: tabular-nums;
  }}
  tfoot td:first-child {{ text-align: left; font-size: .72rem; text-transform: uppercase; letter-spacing: .6px; color: #4b5563; }}
  .chip {{ display: inline-block; padding: 3px 9px; border-radius: 5px; font-size: .75rem; font-weight: 700; }}
  .cg {{ background: #f0fdf4; color: #15803d; border: 1px solid #bbf7d0; }}
  .co {{ background: #fff7ed; color: #c2410c; border: 1px solid #fed7aa; }}
  .cr {{ background: #fef2f2; color: #dc2626; border: 1px solid #fecaca; }}
  .cx {{ background: #f3f4f6; color: #6b7280; border: 1px solid #e5e7eb; }}
  footer {{ text-align: center; margin-top: 24px; font-size: .75rem; color: #6b7280; }}
  .sup-table-wrap {{ background:#fff; border:1px solid #e5e7eb; border-radius:14px; overflow:hidden; margin-bottom:20px; }}
  .sup-table-head {{ padding:14px 18px 13px; border-bottom:1px solid #e5e7eb; font-size:.82rem; font-weight:700; text-transform:uppercase; letter-spacing:.7px; color:#111827; }}
  .sup-name-cell {{ text-align:left !important; font-weight:700 !important; padding-left:14px !important; }}
  .act-badge {{ display:inline-block; margin-left:6px; padding:2px 7px; border-radius:4px; font-size:.7rem; font-weight:700; background:#fef3c7; color:#92400e; border:1px solid #fde68a; }}
  .sup-total td {{ background:#f5f6f8; font-weight:700; border-top:2px solid #e5e7eb; }}
  .sup-total td:first-child {{ font-size:.72rem; text-transform:uppercase; letter-spacing:.6px; color:#4b5563; }}
</style>
</head>
<body>

<div class="topbar">
  <div class="topbar-left">
    <div class="logo-mark">OB</div>
    <div>
      <h1>Outbound Call Report</h1>
      <p>{date_label}{(' &nbsp;·&nbsp; ' + time_window) if time_window else ''} &nbsp;·&nbsp; {n_counsellors} active counsellors &nbsp;·&nbsp; {len(teams)} teams</p>
    </div>
  </div>
  <span class="pill">Outbound only</span>
</div>

<div class="sup-table-wrap">
  <div class="sup-table-head">Supervisor Summary</div>
  <table>
    <thead><tr>
      <th style="text-align:left">Supervisor</th>
      <th>Counsellors</th><th>Calls</th><th>Connected</th>
      <th>Connect&nbsp;%</th><th>Talk&nbsp;Time</th><th>Avg&nbsp;/&nbsp;Call</th>
      <th>Talk&nbsp;/&nbsp;Active&nbsp;(5+)</th>
    </tr></thead>
    <tbody>{sup_rows_html}</tbody>
  </table>
</div>

<div class="kpis">
  <div class="kpi-card" style="--ac:#e5e7eb"><div class="v">{g_total}</div><div class="l">Total calls</div></div>
  <div class="kpi-card" style="--ac:#22c55e;--vc:#16a34a"><div class="v">{g_ans}</div><div class="l">Connected</div></div>
  <div class="kpi-card" style="--ac:#3b82f6;--vc:#2563eb"><div class="v">{g_rate}%</div><div class="l">Connect rate</div></div>
  <div class="kpi-card" style="--ac:#e5e7eb"><div class="v">{fmt_talk(g_talk)}</div><div class="l">Total talk time</div></div>
  <div class="kpi-card" style="--ac:#e5e7eb"><div class="v">{fmt_avg(g_avg)}</div><div class="l">Avg / connected</div></div>
  <div class="kpi-card" style="--ac:#f59e0b;--vc:#b45309"><div class="v">{fmt_talk(g_talk_per_act)}</div><div class="l">Talk / counsellor (5+ calls) · {active_5plus}</div></div>
</div>

<div class="layout">
  <div class="col">{left_html}</div>
  <div class="col">{right_html}</div>
</div>

<footer>Source: {source_note} &nbsp;·&nbsp; Outbound calls only &nbsp;·&nbsp; {date_label}</footer>
</body>
</html>'''


# ─── Screenshot ──────────────────────────────────────────────────────────────

async def take_screenshots(html_path: str, base_path: str):
    """Take 2 screenshots: top section (supervisor table + KPIs) and bottom (team cards)."""
    log('Screenshot browser launching ...')
    async with async_playwright() as p:
        browser  = await p.chromium.launch(headless=True)
        page     = await browser.new_page(viewport={'width': 1400, 'height': 900}, device_scale_factor=2)
        file_url = 'file:///' + html_path.replace('\\', '/')
        log(f'Loading HTML for screenshot: {file_url}')
        await page.goto(file_url)
        await page.wait_for_load_state('networkidle')
        await page.wait_for_timeout(1500)

        full_height = await page.evaluate('document.body.scrollHeight')
        log(f'Page height: {full_height}px — resizing viewport ...')
        await page.set_viewport_size({'width': 1400, 'height': full_height})
        await page.wait_for_timeout(300)

        layout_box  = await page.locator('.layout').bounding_box()
        split_y     = int(layout_box['y'])
        log(f'Split point at y={split_y}px')

        png1 = base_path.replace('.png', '_1.png')
        png2 = base_path.replace('.png', '_2.png')

        await page.screenshot(path=png1, clip={'x': 0, 'y': 0,       'width': 1400, 'height': split_y})
        log(f'Screenshot 1 saved → {png1}')
        await page.screenshot(path=png2, clip={'x': 0, 'y': split_y, 'width': 1400, 'height': full_height - split_y})
        log(f'Screenshot 2 saved → {png2}')

        await browser.close()
    return png1, png2


# ─── WhatsApp send ────────────────────────────────────────────────────────────

def send_whatsapp_image(img_path: str, caption: str, group_id: str, token: str):
    url = 'https://gate.whapi.cloud/messages/image'
    with open(img_path, 'rb') as f:
        resp = requests.post(
            url,
            headers={'Authorization': f'Bearer {token}'},
            data={'to': group_id, 'caption': caption},
            files={'media': (os.path.basename(img_path), f, 'image/png')},
            timeout=60,
        )
    resp.raise_for_status()
    print(f'WhatsApp image sent → {group_id}: {resp.status_code}')
    return resp.json()


def send_whatsapp_html(html_path: str, caption: str, group_id: str, token: str):
    url = 'https://gate.whapi.cloud/messages/document'
    with open(html_path, 'rb') as f:
        resp = requests.post(
            url,
            headers={'Authorization': f'Bearer {token}'},
            data={'to': group_id, 'caption': caption},
            files={'document': (os.path.basename(html_path), f, 'text/html')},
            timeout=60,
        )
    resp.raise_for_status()
    print(f'WhatsApp sent → {group_id}: {resp.status_code}')
    return resp.json()


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    log('=== Outbound Report — START ===')

    # Load CSV — from file or via download
    if args.csv:
        csv_path = os.path.abspath(args.csv)
        log(f'Reading CSV from file: {csv_path}')
        with open(csv_path, encoding='utf-8-sig', errors='replace') as f:
            csv_text = f.read()
        log(f'CSV loaded ({len(csv_text):,} chars)')
    else:
        log('No --csv supplied — downloading from CallInsight ...')
        csv_text = await download_csv_playwright()
        log(f'Download complete ({len(csv_text):,} chars)')

    # Auto-detect date from CSV if --date not given
    effective_date_str = report_date_str
    effective_dt       = report_dt
    if not args.date:
        for row in csv.DictReader(io.StringIO(csv_text)):
            d = parse_date_cell(row.get('Date', ''))
            if re.match(r'\d{2}/\d{2}/\d{4}', d):
                effective_date_str = d
                effective_dt = datetime.strptime(d, '%d/%m/%Y')
                break
        log(f'Auto-detected date from CSV: {effective_date_str}')
    else:
        log(f'Using --date override: {effective_date_str}')

    eff_label = effective_dt.strftime('%#d %B %Y') if sys.platform == 'win32' else effective_dt.strftime('%-d %B %Y')
    eff_stamp = effective_dt.strftime('%d%m%Y_%H%M')
    log(f'Report date: {effective_date_str}  ({eff_label})')

    # Time window
    from_time = args.from_time
    to_time   = args.to_time
    if from_time or to_time:
        ft = from_time or '00:00'
        tt = to_time   or 'now'
        time_window = f'{ft} – {tt}'
        log(f'Time window filter: {time_window}')
    else:
        time_window = ''

    # Process
    log('Parsing CSV rows ...')
    stats       = process_csv(csv_text, effective_date_str, from_time=from_time, to_time=to_time)
    active      = sum(1 for s in stats.values() if s['total'] > 0)
    total_calls = sum(s['total']    for s in stats.values())
    total_ans   = sum(s['answered'] for s in stats.values())
    log(f'Parsed: {total_calls} outbound calls, {total_ans} connected, {active} active counsellors')

    # Build HTML
    log('Building HTML report ...')
    html_content  = build_html(stats, eff_label, csv_source=args.csv or 'CallInsight', time_window=time_window)
    slug          = f'_{from_time.replace(":", "")}-{to_time.replace(":", "")}' if from_time or to_time else ''
    html_filename = f'Outbound_Report_{eff_stamp}{slug}.html'
    html_path     = os.path.join(OUTPUT_DIR, html_filename)
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    log(f'HTML saved → {html_path}')

    if LOCAL_MODE:
        log('--local mode: taking screenshots (no WhatsApp send)')
        png_base = html_path.replace('.html', '.png')
        await take_screenshots(html_path, png_base)
        log('=== Done (local mode) ===')
        return

    target_group = args.group or os.getenv('WHATSAPP_GROUP_CALL_REPORTS', WHATSAPP_GROUP)
    report_type  = 'Cumulative' if (not from_time or from_time == '09:30') else 'Last Hour'
    caption1 = (
        f'📞 Outbound Report — {report_type}'
        + f'\n📅 {eff_label}'
        + (f'\n⏱ {time_window}' if time_window else '')
        + f'\n✅ {total_ans}/{total_calls} connected'
        + f'\n1/2 — Supervisor Summary & KPIs'
    )
    caption2 = (
        f'📞 Outbound Report — {report_type}'
        + f'\n📅 {eff_label}'
        + (f'\n⏱ {time_window}' if time_window else '')
        + f'\n2/2 — Team Breakdown'
    )

    log('Taking screenshots ...')
    png_base = html_path.replace('.html', '.png')
    png1, png2 = await take_screenshots(html_path, png_base)

    log(f'Sending image 1/2 to WhatsApp group {target_group} ...')
    send_whatsapp_image(png1, caption1, target_group, WHAPI_TOKEN)
    log(f'Sending image 2/2 to WhatsApp group {target_group} ...')
    send_whatsapp_image(png2, caption2, target_group, WHAPI_TOKEN)
    log('=== Done ===')


if __name__ == '__main__':
    asyncio.run(main())
