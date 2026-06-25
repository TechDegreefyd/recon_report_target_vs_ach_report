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
import csv
from datetime import datetime, timedelta, UTC
from dotenv import load_dotenv
from playwright.async_api import async_playwright
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
parser.add_argument('--file',      default=None,        help='Path to local XLSX or CSV — skip login/scrape entirely')
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


# ─── Greeter scraper (requests-based) ────────────────────────────────────────

CALL_LOG_URL = 'https://greeter.co.in/reseller/call_log'
EXPORT_URL   = 'https://greeter.co.in/export_call_log_data/xlsx'


def _extract_csrf(html: str) -> str:
    for line in html.splitlines():
        if 'csrf' in line.lower() and 'value' in line.lower():
            m = re.search(r'value=["\']([^"\']{20,})["\']', line)
            if m:
                return m.group(1)
    return ''


async def fetch_greeter_xlsx_playwright(target_date_str: str, explore: bool = False) -> str:
    """
    Use Playwright to login, set the date range picker to target_date_str (DD/MM/YYYY),
    and download the XLSX. Works for any date including historical dates.
    """
    dt         = datetime.strptime(target_date_str, '%d/%m/%Y')
    # Greeter date picker uses DD/MM/YYYY format in its input fields
    date_input = dt.strftime('%d/%m/%Y')   # e.g. 24/06/2026
    api_date   = dt.strftime('%Y-%m-%d')

    DOWNLOAD_DIR = os.path.join(_DIR, 'greeter_downloads')
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    xlsx_path = os.path.join(DOWNLOAD_DIR, f'greeter_{RUN_STAMP}.xlsx')

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx     = await browser.new_context(accept_downloads=True)
        page    = await ctx.new_page()

        # ── Login ────────────────────────────────────────────────────────────
        log('Playwright: navigating to login …')
        await page.goto(GREETER_URL, wait_until='networkidle')
        await page.fill('input[name="username"]', GREETER_USERNAME)
        await page.fill('input[name="password"]', GREETER_PASSWORD)
        async with page.expect_navigation(wait_until='networkidle'):
            await page.click('button[type="submit"], input[type="submit"]')
        log(f'Playwright: logged in → {page.url}')

        # ── Navigate to call log ─────────────────────────────────────────────
        log('Playwright: navigating to call log …')
        await page.goto(CALL_LOG_URL, wait_until='networkidle')
        await page.wait_for_timeout(1500)

        if explore:
            html = await page.content()
            explore_path = os.path.join(_DIR, 'explore_dump.html')
            with open(explore_path, 'w', encoding='utf-8') as f:
                f.write(html)
            log(f'EXPLORE: page HTML dumped → {explore_path}')
            await browser.close()
            return ''

        # ── Set date range ───────────────────────────────────────────────────
        # Try common date range input selectors used by Greeter
        log(f'Playwright: setting date range to {date_input} …')

        # Many Django/Bootstrap date range pickers use inputs with id containing "start"/"end" or "from"/"to"
        # Try filling start_date and end_date inputs
        for sel in ['#start_date', 'input[name="start_date"]', 'input[placeholder*="Start"]',
                    'input[placeholder*="From"]', 'input[placeholder*="start"]']:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    await loc.first.triple_click()
                    await loc.first.fill(date_input)
                    log(f'  Filled start date via {sel}')
                    break
            except Exception:
                pass

        for sel in ['#end_date', 'input[name="end_date"]', 'input[placeholder*="End"]',
                    'input[placeholder*="To"]', 'input[placeholder*="end"]']:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    await loc.first.triple_click()
                    await loc.first.fill(date_input)
                    log(f'  Filled end date via {sel}')
                    break
            except Exception:
                pass

        # Press Enter or click a filter/submit button
        for sel in ['button[type="submit"]', 'input[type="submit"]',
                    'button:has-text("Filter")', 'button:has-text("Search")',
                    'button:has-text("Apply")', '#filter_btn', '#search_btn']:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    await loc.first.click()
                    log(f'  Clicked filter button via {sel}')
                    break
            except Exception:
                pass

        await page.wait_for_timeout(2000)

        # ── Click export/download button ─────────────────────────────────────
        log('Playwright: triggering XLSX export …')
        downloaded = False
        for sel in ['a:has-text("Export")', 'a:has-text("Download")', 'a:has-text("xlsx")',
                    'a:has-text("Excel")', 'button:has-text("Export")', '#export_btn',
                    'a[href*="export"]', 'a[href*="xlsx"]']:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    async with page.expect_download(timeout=30000) as dl_info:
                        await loc.first.click()
                    download = await dl_info.value
                    await download.save_as(xlsx_path)
                    log(f'Downloaded via UI → {xlsx_path}  ({os.path.getsize(xlsx_path):,} bytes)')
                    downloaded = True
                    break
            except Exception as e:
                log(f'  Export selector {sel} failed: {e}')

        if not downloaded:
            # Fallback: POST the export URL directly using cookies from Playwright session
            log('Playwright: falling back to cookie-based POST export …')
            cookies = await ctx.cookies()
            cookie_str = '; '.join(f'{c["name"]}={c["value"]}' for c in cookies)

            # Get CSRF from page
            csrf2 = ''
            try:
                csrf2 = await page.evaluate(
                    "document.querySelector('input[name=csrfmiddlewaretoken]')?.value || ''"
                )
            except Exception:
                pass
            if not csrf2:
                csrf2 = _extract_csrf(await page.content())

            session = requests.Session()
            session.headers.update({
                'User-Agent': 'Mozilla/5.0',
                'Cookie':     cookie_str,
                'Referer':    CALL_LOG_URL,
            })
            export_payload = {'start_date': api_date, 'end_date': api_date}
            if csrf2:
                export_payload['csrfmiddlewaretoken'] = csrf2

            r = session.post(EXPORT_URL, data=export_payload, timeout=60, stream=True)
            ct = r.headers.get('Content-Type', '')
            if r.status_code == 200 and any(k in ct for k in ('spreadsheet', 'octet-stream', 'excel')):
                with open(xlsx_path, 'wb') as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
                log(f'Downloaded via cookie POST → {xlsx_path}  ({os.path.getsize(xlsx_path):,} bytes)')
            else:
                raise RuntimeError(f'Export failed: {r.status_code} {ct}\n{r.text[:300]}')

        await browser.close()

    return xlsx_path


def fetch_greeter_xlsx(target_date_str: str) -> str:
    """Sync wrapper kept for compatibility — delegates to Playwright version."""
    return asyncio.get_event_loop().run_until_complete(
        fetch_greeter_xlsx_playwright(target_date_str)
    )


def _clean_agent_name(raw: str) -> str:
    """'Tanya .' → 'Tanya',  'Khushi Mehta .' → 'Khushi Mehta'"""
    return ' '.join(raw.strip().rstrip('.').split())


def parse_csv(csv_path: str) -> list[dict]:
    log(f'Parsing CSV: {csv_path}')
    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        data = [row for row in reader if any(v.strip() for v in row.values())]
    log(f'CSV rows: {len(data)}')
    if data:
        log(f'CSV headers: {list(data[0].keys())}')
        log(f'Sample: {data[0]}')
    return data


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
    color: #121212; font-size: .92rem; font-weight: 900;
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

    if args.file:
        log(f'Using local file: {args.file}')
        if args.file.lower().endswith('.csv'):
            rows = parse_csv(args.file)
        else:
            rows = parse_xlsx(args.file)
    else:
        xlsx_path = await fetch_greeter_xlsx_playwright(report_date_str, explore=args.explore)
        if args.explore:
            log('=== Explore done — check explore_dump.html ===')
            return
        rows = parse_xlsx(xlsx_path)

    if not rows:
        log('WARNING: No data rows found. The report will be empty.')

    # Greeter's export API often ignores date params — filter client-side
    target_api_date = report_dt.strftime('%Y-%m-%d')
    rows_before = len(rows)
    rows = [r for r in rows if r.get('Date', '').startswith(target_api_date)]
    log(f'Date filter ({target_api_date}): {rows_before} → {len(rows)} rows')

    if not rows:
        log('WARNING: No rows matched target date after filtering.')

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
