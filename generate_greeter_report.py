#!/usr/bin/env python3
"""
Greeter.co.in Report — Login, scrape Advanced Search data, build HTML, send via WhatsApp.
Usage:  python generate_greeter_report.py [--local] [--date yesterday|today|DD/MM/YYYY]
                                          [--explore]  <- dump page HTML for debugging
  --local    skip WhatsApp send, just save the HTML + screenshots
  --date     yesterday (default) | today | DD/MM/YYYY
  --explore  open browser, click date range, dump inner HTML, exit — useful for debugging selectors
"""

import asyncio
import os
import sys
import re
import argparse
import time
from datetime import datetime, timedelta, UTC
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
import requests
import openpyxl

_START = time.perf_counter()

def log(msg: str):
    elapsed = time.perf_counter() - _START
    print(f'[{elapsed:6.1f}s] {msg}', flush=True)

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# ─── Args ─────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(add_help=False)
parser.add_argument('--local',     action='store_true')
parser.add_argument('--explore',   action='store_true', help='Dump page HTML then exit')
parser.add_argument('--date',      default=None,        help='today | DD/MM/YYYY (default: today)')
parser.add_argument('--group',     default=None,        help='Override WhatsApp group ID')
parser.add_argument('--from-time', default=None,        dest='from_time', help='HH:MM — filter calls from this time')
parser.add_argument('--to-time',   default=None,        dest='to_time',   help='HH:MM — filter calls up to this time (exclusive)')
parser.add_argument('--nocleanup', action='store_true', help='Skip auto-cleanup of old downloads and reports')
args, _ = parser.parse_known_args()

LOCAL_MODE = args.local

# ─── Paths / env ──────────────────────────────────────────────────────────────
_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_DIR, '.env'))

OUTPUT_DIR = os.path.join(_DIR, 'Automation Cron Job', 'Greeter Report')
os.makedirs(OUTPUT_DIR, exist_ok=True)

WHAPI_TOKEN   = os.getenv('WHAPI_TOKEN_PAID')
WHATSAPP_GROUP = os.getenv('WHATSAPP_GROUP', '120363426619711887@g.us')

# ─── Credentials ──────────────────────────────────────────────────────────────
GREETER_URL      = 'https://greeter.co.in/login'
GREETER_USERNAME = os.getenv('GREETER_USERNAME')
GREETER_PASSWORD = os.getenv('GREETER_PASSWORD')

# ─── Date logic ───────────────────────────────────────────────────────────────
now_ist = datetime.now(UTC) + timedelta(hours=5, minutes=30)

_date_arg = args.date or 'today'

if _date_arg == 'today':
    report_dt      = now_ist
    date_btn_label = 'Today'
elif _date_arg == 'yesterday':
    report_dt      = now_ist - timedelta(days=1)
    date_btn_label = 'Yesterday'
else:
    report_dt      = datetime.strptime(_date_arg, '%d/%m/%Y')
    date_btn_label = 'Today'

report_date_str = report_dt.strftime('%d/%m/%Y')
date_label      = report_dt.strftime('%#d %B %Y') if sys.platform == 'win32' else report_dt.strftime('%-d %B %Y')
RUN_STAMP       = now_ist.strftime('%d%m%Y_%H%M')


# ─── Greeter scraper ──────────────────────────────────────────────────────────

CALL_LOG_URL = 'https://greeter.co.in/reseller/call_log'


async def scrape_greeter(target_date_str: str, date_btn: str, explore: bool = False) -> str:
    """
    Login → navigate directly to /reseller/call_log → click date button → scrape table.
    """
    log('Launching browser …')
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not explore)
        ctx     = await browser.new_context(viewport={'width': 1600, 'height': 900})
        page    = await ctx.new_page()

        # ── Login ─────────────────────────────────────────────────────────────
        log(f'Opening login page …')
        await page.goto(GREETER_URL, wait_until='networkidle', timeout=60_000)
        await page.wait_for_timeout(800)

        log('Filling login form …')
        await page.wait_for_selector('input[name="username"]', timeout=15_000)
        await page.fill('input[name="username"]', GREETER_USERNAME)
        await page.fill('input[name="password"]', GREETER_PASSWORD)
        await page.wait_for_timeout(500)

        # Click whichever login button is present
        for btn_sel in ['button:has-text("Login")', 'button:has-text("Sign in")',
                        'input[type="submit"]', 'button[type="submit"]']:
            try:
                el = page.locator(btn_sel).first
                if await el.is_visible(timeout=2000):
                    await el.click()
                    log(f'  Clicked: {btn_sel}')
                    break
            except Exception:
                continue

        await page.wait_for_load_state('networkidle', timeout=30_000)
        await page.wait_for_timeout(1500)
        log(f'Logged in → {page.url}')

        # ── Navigate to Call Log ───────────────────────────────────────────────
        log(f'Navigating to {CALL_LOG_URL} …')
        await page.goto(CALL_LOG_URL, wait_until='networkidle', timeout=30_000)
        await page.wait_for_timeout(2000)
        log(f'Call Log page loaded → {page.url}')

        if explore:
            await _save_explore(page, 'greeter_explore_calllog_before_date')

        # ── Click the date range button (Yesterday / Today / etc.) ────────────
        date_button_selectors = [
            f'button:has-text("{date_btn}")',
            f'a:has-text("{date_btn}")',
            f'input[value="{date_btn}"]',
            f'[class*="btn"]:has-text("{date_btn}")',
            f'span:has-text("{date_btn}")',
        ]

        date_clicked = False
        for sel in date_button_selectors:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=3000):
                    log(f'Clicking date button: {sel!r}')
                    await el.click()
                    await page.wait_for_load_state('networkidle', timeout=20_000)
                    await page.wait_for_timeout(2500)
                    date_clicked = True
                    break
            except Exception:
                continue

        if not date_clicked:
            log(f'WARNING: Could not click "{date_btn}" button — reading current data')

        # ── Wait for DataTable to finish loading after date button click ─────
        log('Waiting for table to finish loading …')
        try:
            # DataTables shows a "Processing…" div while loading — wait for it to hide
            await page.wait_for_selector('#Call_log_table_processing',
                                         state='hidden', timeout=20_000)
        except Exception:
            await page.wait_for_timeout(3000)

        if explore:
            await _save_explore(page, 'greeter_explore_calllog_after_date', full_page=True)
            log('[EXPLORE] Done — inspect greeter_explore_calllog_after_date.html')

        # ── Click Export and capture the download ──────────────────────────────
        DOWNLOAD_DIR = os.path.join(_DIR, 'greeter_downloads')
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        xlsx_path = os.path.join(DOWNLOAD_DIR, f'greeter_{RUN_STAMP}.xlsx')

        log('Clicking Export button …')
        await page.wait_for_selector('button#nofilter', state='visible', timeout=10_000)
        async with page.expect_download(timeout=60_000) as dl_info:
            await page.click('button#nofilter')

        download = await dl_info.value
        await download.save_as(xlsx_path)
        log(f'Downloaded → {xlsx_path}  ({os.path.getsize(xlsx_path):,} bytes)')

        await browser.close()

    return xlsx_path


async def _save_explore(page, name: str, full_page: bool = False):
    html = await page.content()
    html_path = os.path.join(_DIR, f'{name}.html')
    png_path  = os.path.join(_DIR, f'{name}.png')
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html)
    await page.screenshot(path=png_path, full_page=full_page)
    log(f'[EXPLORE] Saved → {html_path}  {png_path}')


def _clean_agent_name(raw: str) -> str:
    """'Tanya .' → 'Tanya',  'Khushi Mehta .' → 'Khushi Mehta'"""
    return ' '.join(raw.strip().rstrip('.').split())


def parse_xlsx(xlsx_path: str) -> list[dict]:
    """
    Parse the exported XLSX.
    Columns: Id | Client Name | Virtual No | Caller ID | Call Type |
             DID Description | Caller | Caller Name | Group |
             Receiver | Receiver Name | Date | Total Duration |
             Answered Duration | Status
    """
    log(f'Parsing XLSX: {xlsx_path}')
    wb   = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws   = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        log('XLSX is empty!')
        return []

    headers = [str(h).strip() if h is not None else '' for h in rows[0]]
    log(f'XLSX headers: {headers}')

    data = []
    for row in rows[1:]:
        if all(v is None for v in row):
            continue
        d = dict(zip(headers, [str(v).strip() if v is not None else '' for v in row]))
        data.append(d)

    log(f'XLSX rows: {len(data)}')
    if data:
        log(f'Sample: {data[0]}')
    return data


# ─── Process scraped rows → per-counsellor stats ──────────────────────────────

# Map greeter agent/counsellor names → teams
# Update this to match actual names on the greeter platform
COUNSELLOR_TEAM_MAP = {
    # 'Agent Name on Greeter': 'Team Name',
    # Example entries — replace with real data from --explore run:
    'Vishwajeet':        'Sunil Team',
    'Arnav':             'Vartika Team',
    'Avneet':            'Sunil Team',
    'Himanshi':          'Sunil Team',
    'Vikas':             'Sunil Team',
    'Abhishek':          'Sunil Team',
    'Preeti':            'Sunil Team',
    'Prashant':          'Varun Team',
    'Anshika':           'Varun Team',
    'Tanisha':           'Varun Team',
    'Virat':             'Sid Team',
    'LaxmiNarayan':      'Sid Team',
    'Sanjay':            'Vartika Team',
    'Shiv':              'Vartika Team',
    'Sagar':             'Vartika Team',
    'Nitin':             'Vartika Team',
    'Aditya':            'Vartika Team',
    'Swapnil':           'Prashant Team',
    'Divya':             'Prashant Team',
    'Tanya':             'Sid Team',
    'Suhani':            'Sid Team',
    'Kuldeep':           'Sid Team',
    'Om':                'Varun Team',
    'Abhishek Dubey':    'Prashant Team',
    'Mohit':             'Prashant Team',
    'Abhishek Kamat':    'Vartika Team',
    'Neha':              'Sunil Team',
    'Navneet':           'Sunil Team',
    'Akshay':            'Sunil Team',
    'Abhishek Sikarwar': 'Varun Team',
    'Divya Goel':        'Varun Team',
}

TEAM_ORDER = ['Vartika Team', 'Sunil Team', 'Sid Team', 'Varun Team', 'Prashant Team']
TEAM_COLORS = {
    'Vartika Team':  '#8b5cf6',
    'Sunil Team':    '#0ea5e9',
    'Sid Team':      '#f59e0b',
    'Varun Team':    '#10b981',
    'Prashant Team': '#ec4899',
}


def _parse_duration(val: str) -> int:
    """HH:MM:SS or MM:SS → seconds."""
    parts = str(val).strip().split(':')
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, TypeError):
        pass
    return 0


def _extract_time_hhmm(date_val: str) -> str:
    """Extract HH:MM from date strings like '22/06/2025 14:30:45' or '2025-06-22 14:30:45'."""
    date_val = (date_val or '').strip()
    # Look for a time component HH:MM anywhere in the string
    m = re.search(r'(\d{2}):(\d{2})', date_val)
    if m:
        return f'{m.group(1)}:{m.group(2)}'
    return ''


def process_rows(rows: list[dict], from_time: str = None, to_time: str = None) -> dict:
    """
    Build per-agent pivot stats from XLSX rows.
    from_time / to_time: 'HH:MM' strings; window is [from_time, to_time).
    Returns dict keyed by agent name with:
      answered_count, no_answered_count, total_count
      ans_total_secs, ans_answered_secs   (for Answered calls)
      no_total_secs,  no_answered_secs    (for No Answered calls)
    """
    stats: dict[str, dict] = {}
    skipped_time = 0

    for row in rows:
        # ── Time window filter ────────────────────────────────────────────────
        if from_time or to_time:
            row_time = _extract_time_hhmm(row.get('Date', '') or '')
            if from_time and row_time < from_time:
                skipped_time += 1
                continue
            if to_time and row_time >= to_time:
                skipped_time += 1
                continue

        agent_raw = _clean_agent_name(row.get('Receiver Name', '') or '')
        if not agent_raw or agent_raw == 'None':
            continue

        status       = (row.get('Status', '') or '').strip()
        total_secs   = _parse_duration(row.get('Total Duration', '') or '')
        ans_secs     = _parse_duration(row.get('Answered Duration', '') or '')
        is_answered  = (status == 'Answered')

        if agent_raw not in stats:
            stats[agent_raw] = {
                'answered_count':   0,
                'no_answered_count': 0,
                'ans_total_secs':   0,
                'ans_answered_secs': 0,
                'no_total_secs':    0,
                'no_answered_secs': 0,
            }

        s = stats[agent_raw]
        if is_answered:
            s['answered_count']    += 1
            s['ans_total_secs']    += total_secs
            s['ans_answered_secs'] += ans_secs
        else:
            s['no_answered_count'] += 1
            s['no_total_secs']     += total_secs
            s['no_answered_secs']  += ans_secs

    # Add computed totals
    for s in stats.values():
        s['total_count']        = s['answered_count'] + s['no_answered_count']
        s['overall_total_secs'] = s['ans_total_secs'] + s['no_total_secs']
        s['overall_ans_secs']   = s['ans_answered_secs'] + s['no_answered_secs']

    if skipped_time:
        log(f'  Time filter skipped {skipped_time} rows outside [{from_time or "start"} – {to_time or "end"})')

    return stats


# ─── HTML generation ─────────────────────────────────────────────────────────

def fmt_dur(secs: int) -> str:
    """Format seconds as HH:MM:SS."""
    if secs is None:
        secs = 0
    h, rem = divmod(int(secs), 3600)
    m, s   = divmod(rem, 60)
    return f'{h:02d}:{m:02d}:{s:02d}'


def fmt_talk(secs: int) -> str:
    if not secs:
        return '—'
    m = round(secs / 60)
    h, mn = divmod(m, 60)
    return f'{h}h {mn:02d}m' if h else f'{m}m'


def build_html(stats: dict, date_label: str, raw_rows: list[dict] = None, time_window: str = '') -> str:
    agents = sorted(stats.keys())
    n      = len(agents)

    # Grand totals
    gt_ans    = sum(s['answered_count']    for s in stats.values())
    gt_no     = sum(s['no_answered_count'] for s in stats.values())
    gt_total  = gt_ans + gt_no
    gt_ans_td = sum(s['ans_total_secs']    for s in stats.values())
    gt_ans_ad = sum(s['ans_answered_secs'] for s in stats.values())
    gt_no_td  = sum(s['no_total_secs']     for s in stats.values())
    gt_no_ad  = sum(s['no_answered_secs']  for s in stats.values())
    gt_ov_td  = gt_ans_td + gt_no_td
    gt_ov_ad  = gt_ans_ad + gt_no_ad
    conn_rate = round(gt_ans / gt_total * 100) if gt_total else 0

    def chip_rate(pct, total):
        if not total:
            return '<span class="chip cx">—</span>'
        cls = 'cg' if pct >= 45 else ('co' if pct >= 30 else 'cr')
        return f'<span class="chip {cls}">{pct}%</span>'

    # Build pivot rows
    rows_html = ''
    for agent in agents:
        s    = stats[agent]
        rate = round(s['answered_count'] / s['total_count'] * 100) if s['total_count'] else 0
        rows_html += f'''<tr>
      <td style="text-align:left;font-weight:700;padding-left:14px">{agent}</td>
      <td>{s["answered_count"]}</td>
      <td>{s["no_answered_count"]}</td>
      <td><strong>{s["total_count"]}</strong></td>
      <td>{chip_rate(rate, s["total_count"])}</td>
      <td>{fmt_dur(s["ans_total_secs"])}</td>
      <td>{fmt_dur(s["ans_answered_secs"])}</td>
      <td>{fmt_dur(s["no_total_secs"])}</td>
      <td>{fmt_dur(s["overall_total_secs"])}</td>
      <td>{fmt_dur(s["overall_ans_secs"])}</td>
    </tr>'''

    gt_rate = round(gt_ans / gt_total * 100) if gt_total else 0

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Greeter Report · {date_label}</title>
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
  .kpis {{ display: grid; grid-template-columns: repeat(5,1fr); gap: 10px; margin-bottom: 28px; }}
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
  .tcard  {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 14px; overflow: hidden; margin-bottom: 0; }}
  .tcard-head {{
    padding: 14px 18px 13px; border-bottom: 1px solid #f3f4f6;
    display: flex; align-items: center; justify-content: space-between;
  }}
  .tcard-title {{ font-size: .82rem; font-weight: 700; text-transform: uppercase; letter-spacing: .7px; color: #111827; }}
  .tcard-sub   {{ font-size: .8rem; color: #4b5563; }}
  table {{ width: 100%; border-collapse: collapse; }}
  thead th {{
    padding: 7px 12px;
    font-size: .78rem; font-weight: 700; text-transform: uppercase; letter-spacing: .55px;
    color: #4b5563; text-align: right; white-space: nowrap;
    background: #F5F6F8; border-bottom: 1px solid #e5e7eb;
  }}
  thead th:first-child {{ text-align: left; }}
  thead tr.grp-row th {{
    padding: 6px 12px; font-size: .72rem; font-weight: 800;
    text-transform: uppercase; letter-spacing: .6px;
    text-align: center; border-bottom: 1px solid #e5e7eb;
  }}
  tbody td {{
    padding: 8px 12px; text-align: right;
    color: #121212; font-size: .92rem; font-weight: 500;
    border-bottom: 1px solid #f3f4f6;
    font-variant-numeric: tabular-nums;
  }}
  tbody tr:hover td {{ background: #f9fafb; }}
  tbody tr:last-child td {{ border-bottom: none; }}
  tfoot tr {{ background: #f5f6f8; }}
  tfoot td {{
    padding: 9px 12px; text-align: right;
    font-size: .88rem; font-weight: 700; color: #1f2937;
    border-top: 2px solid #e5e7eb;
    font-variant-numeric: tabular-nums;
  }}
  tfoot td:first-child {{ text-align: left; font-size: .78rem; text-transform: uppercase; letter-spacing: .6px; color: #4b5563; padding-left: 14px; }}
  .chip {{ display: inline-block; padding: 3px 9px; border-radius: 5px; font-size: .82rem; font-weight: 700; }}
  .cg {{ background: #f0fdf4; color: #15803d; border: 1px solid #bbf7d0; }}
  .co {{ background: #fff7ed; color: #c2410c; border: 1px solid #fed7aa; }}
  .cr {{ background: #fef2f2; color: #dc2626; border: 1px solid #fecaca; }}
  .cx {{ background: #f3f4f6; color: #6b7280; border: 1px solid #e5e7eb; }}
  footer {{ text-align: center; margin-top: 24px; font-size: .75rem; color: #6b7280; }}
</style>
</head>
<body>

<div class="topbar">
  <div class="topbar-left">
    <div class="logo-mark">GR</div>
    <div>
      <h1>Greeter Call Log Report</h1>
      <p>{date_label}{(' &nbsp;·&nbsp; ' + time_window) if time_window else ''} &nbsp;·&nbsp; {n} active agents &nbsp;·&nbsp; {gt_total} total calls</p>
    </div>
  </div>
  <span class="pill">greeter.co.in</span>
</div>

<div class="kpis">
  <div class="kpi-card" style="--ac:#22c55e;--vc:#16a34a"><div class="v">{gt_ans}</div><div class="l">Answered</div></div>
  <div class="kpi-card" style="--ac:#ef4444;--vc:#dc2626"><div class="v">{gt_no}</div><div class="l">Not Answered</div></div>
  <div class="kpi-card" style="--ac:#e5e7eb"><div class="v">{gt_total}</div><div class="l">Total Calls</div></div>
  <div class="kpi-card" style="--ac:#3b82f6;--vc:#2563eb"><div class="v">{conn_rate}%</div><div class="l">Connect Rate</div></div>
  <div class="kpi-card" style="--ac:#8b5cf6;--vc:#7c3aed"><div class="v">{fmt_dur(gt_ans_ad)}</div><div class="l">Total Talk Time</div></div>
</div>

<div class="tcard">
  <div class="tcard-head">
    <span class="tcard-title">Agent Performance Breakdown</span>
    <span class="tcard-sub">{date_label}</span>
  </div>
  <table>
    <thead>
      <tr class="grp-row">
        <th style="text-align:left;background:#F5F6F8" rowspan="2">Agent</th>
        <th colspan="3" style="background:#fffbeb;color:#92400e;border:1px solid #fde68a">Calls</th>
        <th style="background:#F5F6F8;border:none" rowspan="2">Connect&nbsp;%</th>
        <th colspan="2" style="background:#dcfce7;color:#166534;border:1px solid #bbf7d0">Answered</th>
        <th colspan="1" style="background:#fee2e2;color:#991b1b;border:1px solid #fecaca">Not Answered</th>
        <th colspan="2" style="background:#ede9fe;color:#5b21b6;border:1px solid #ddd6fe">Overall</th>
      </tr>
      <tr>
        <th style="background:#fffbeb;color:#92400e">Answered</th>
        <th style="background:#fee2e2;color:#991b1b">Not Ans.</th>
        <th style="background:#F5F6F8">Total</th>
        <th style="background:#dcfce7;color:#166534">Total&nbsp;Dur.</th>
        <th style="background:#dcfce7;color:#166534">Talk&nbsp;Dur.</th>
        <th style="background:#fee2e2;color:#991b1b">Total&nbsp;Dur.</th>
        <th style="background:#ede9fe;color:#5b21b6">Total&nbsp;Dur.</th>
        <th style="background:#ede9fe;color:#5b21b6">Talk&nbsp;Dur.</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
    <tfoot><tr>
      <td>Grand Total</td>
      <td>{gt_ans}</td>
      <td>{gt_no}</td>
      <td>{gt_total}</td>
      <td>{chip_rate(gt_rate, gt_total)}</td>
      <td>{fmt_dur(gt_ans_td)}</td>
      <td>{fmt_dur(gt_ans_ad)}</td>
      <td>{fmt_dur(gt_no_td)}</td>
      <td>{fmt_dur(gt_ov_td)}</td>
      <td>{fmt_dur(gt_ov_ad)}</td>
    </tr></tfoot>
  </table>
</div>

<footer>Source: greeter.co.in &nbsp;·&nbsp; {date_label}</footer>
</body>
</html>'''


# ─── Cleanup ──────────────────────────────────────────────────────────────────

def cleanup_old_files(max_age_hours: int = 48):
    """Delete XLSX downloads and report files older than max_age_hours."""
    dirs = [
        (os.path.join(_DIR, 'greeter_downloads'),                    ('.xlsx',)),
        (os.path.join(_DIR, 'Automation Cron Job', 'Greeter Report'), ('.html', '.png')),
    ]
    cutoff  = time.time() - max_age_hours * 3600
    deleted = 0
    for d, exts in dirs:
        if not os.path.isdir(d):
            continue
        for fname in os.listdir(d):
            if not any(fname.endswith(e) for e in exts):
                continue
            fpath = os.path.join(d, fname)
            if os.path.getmtime(fpath) < cutoff:
                try:
                    os.remove(fpath)
                    deleted += 1
                except Exception as e:
                    log(f'  cleanup: could not remove {fpath}: {e}')
    if deleted:
        log(f'Cleanup: removed {deleted} file(s) older than {max_age_hours}h')


# ─── Screenshot ───────────────────────────────────────────────────────────────

async def take_screenshots(html_path: str, base_path: str):
    log('Launching screenshot browser …')
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page    = await browser.new_page(viewport={'width': 2200, 'height': 900}, device_scale_factor=2)
        file_url = 'file:///' + html_path.replace('\\', '/')
        await page.goto(file_url, wait_until='domcontentloaded')
        await page.wait_for_timeout(1500)

        full_height = await page.evaluate('document.body.scrollHeight')
        await page.set_viewport_size({'width': 2200, 'height': full_height})
        await page.wait_for_timeout(300)

        card_box = await page.locator('.tcard').bounding_box()
        split_y  = int(card_box['y']) if card_box else full_height // 2

        png1 = base_path.replace('.png', '_1.png')
        png2 = base_path.replace('.png', '_2.png')

        await page.screenshot(path=png1, clip={'x': 0, 'y': 0,       'width': 2200, 'height': split_y})
        await page.screenshot(path=png2, clip={'x': 0, 'y': split_y, 'width': 2200, 'height': full_height - split_y})
        log(f'Screenshots saved → {png1}  {png2}')
        await browser.close()
    return png1, png2


# ─── WhatsApp send ────────────────────────────────────────────────────────────

def send_whatsapp_image(img_path: str, caption: str, group_id: str, token: str):
    import base64
    filename = os.path.basename(img_path)
    with open(img_path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode('utf-8')
    media_data = f'data:image/png;name={filename};base64,{b64}'
    payload = {'to': group_id, 'media': media_data, 'caption': caption}
    headers = {'accept': 'application/json', 'authorization': f'Bearer {token}', 'content-type': 'application/json'}
    for attempt in range(2):
        try:
            resp = requests.post('https://gate.whapi.cloud/messages/image',
                                 headers=headers, json=payload, timeout=20)
            if 200 <= resp.status_code < 300:
                log(f'WhatsApp image sent → {group_id}: {resp.status_code}')
                return
            resp.raise_for_status()
        except Exception as e:
            log(f'WHAPI attempt {attempt+1} failed: {e}')
            if attempt == 0:
                time.sleep(3)


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    log('=== Greeter Report — START ===')

    if not args.nocleanup:
        cleanup_old_files()

    xlsx_path = await scrape_greeter(report_date_str, date_btn_label, explore=args.explore)

    if args.explore:
        log('Explore mode complete. XLSX downloaded, HTML snapshots saved.')
        return

    rows = parse_xlsx(xlsx_path)

    if not rows:
        log('WARNING: No data rows found. The report will be empty.')

    log('Processing rows …')
    stats       = process_rows(rows)
    total_calls = sum(s['total_count']    for s in stats.values())
    total_ans   = sum(s['answered_count'] for s in stats.values())
    active      = sum(1 for s in stats.values() if s['total_count'] > 0)
    log(f'Stats: {total_calls} calls, {total_ans} connected, {active} active agents')

    log('Building HTML …')
    html_content  = build_html(stats, date_label)
    html_filename = f'Greeter_Report_{RUN_STAMP}.html'
    html_path     = os.path.join(OUTPUT_DIR, html_filename)
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    log(f'HTML saved → {html_path}')

    if LOCAL_MODE:
        png_base = html_path.replace('.html', '.png')
        await take_screenshots(html_path, png_base)
        log('=== Done (local mode) ===')
        return

    target_group = args.group or os.getenv('WHATSAPP_GROUP_GREETER', WHATSAPP_GROUP)
    conn_rate = round(total_ans / total_calls * 100) if total_calls else 0
    caption1 = (
        f'📊 Greeter Call Log — {date_label}\n'
        f'✅ {total_ans}/{total_calls} answered ({conn_rate}%)\n'
        f'👥 {active} active agents\n'
        f'1/2 — KPIs'
    )
    caption2 = (
        f'📊 Greeter Call Log — {date_label}\n'
        f'2/2 — Agent Breakdown'
    )

    png_base = html_path.replace('.html', '.png')
    png1, png2 = await take_screenshots(html_path, png_base)

    if WHAPI_TOKEN:
        send_whatsapp_image(png1, caption1, target_group, WHAPI_TOKEN)
        send_whatsapp_image(png2, caption2, target_group, WHAPI_TOKEN)
    else:
        log('WHAPI_TOKEN not set — skipping WhatsApp send')

    for f_path in [html_path, png1, png2]:
        try:
            os.remove(f_path)
        except Exception:
            pass
    log('=== Done ===')


if __name__ == '__main__':
    asyncio.run(main())
