"""
Pull full greeter call log details (duration, caller, receiver, status, recording — same data
as the "Admin > Call Log" page) for one or more Normal IVR DID numbers, for the current month,
via session login using the IVR admin credentials (GREETER_USERNAME_IVR / GREETER_USERNAME_PASSWORD_IVR).

The call log is scoped to ONE DID/virtual number at a time in the server session (the dropdown
top-right in the UI, e.g. "919484958352 - 919484958352"). Switching it calls
adminUserChangePlanData/{user_plan_id}; the rich call log endpoint (admin_call_log_list) then
returns data for whichever number is currently selected in the session. This script logs in,
discovers all DID numbers + plan ids via getAdminAllPlanDetails, then loops over every number
(or just the ones passed via --did), switching the session each time and pulling the month's
call log with full details (parsed out of the HTML-embedded cells the endpoint returns).

Usage:
    python test_greeter_ivr_api.py                       # all numbers, this month
    python test_greeter_ivr_api.py --did 919484958352     # just one number (repeatable)
    python test_greeter_ivr_api.py --csv report.csv       # also write a flat CSV of all rows
"""
import os
import re
import sys
import json
import time
import csv
import argparse
import requests
from dotenv import load_dotenv

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

parser = argparse.ArgumentParser()
parser.add_argument('--did', action='append', default=None,
                     help='DID/virtual number to pull (repeatable). Omit to pull all numbers on the account.')
parser.add_argument('--csv', default=None, help='Also write a flat CSV of all parsed rows to this path.')
args = parser.parse_args()

USERNAME = os.getenv('GREETER_USERNAME_IVR')
PASSWORD = os.getenv('GREETER_USERNAME_PASSWORD_IVR')

BASE_URL         = 'https://greeter.co.in/'
LOGIN_URL        = BASE_URL + 'login'
PLAN_DETAILS_URL = BASE_URL + 'getAdminAllPlanDetails'
CHANGE_PLAN_URL  = BASE_URL + 'adminUserChangePlanData/{}'
CALL_LOG_URL     = BASE_URL + 'admin_call_log_list'
CALL_LOG_PAGE    = BASE_URL + 'admin/call_log'

if not USERNAME or not PASSWORD:
    raise SystemExit('Set GREETER_USERNAME_IVR and GREETER_USERNAME_PASSWORD_IVR env vars first.')


def extract_csrf(html: str) -> str:
    for line in html.splitlines():
        if 'csrf' in line.lower() and 'value' in line.lower():
            m = re.search(r'value=["\']([^"\']{20,})["\']', line)
            if m:
                return m.group(1)
    return ''


def extract_csrf_meta(html: str) -> str:
    m = re.search(r'name="csrf-token"\s+content="([^"]+)"', html)
    return m.group(1) if m else ''


def strip_tags(html: str) -> str:
    return re.sub(r'<[^>]+>', '', html or '').strip()


def parse_row(raw: dict) -> dict:
    """Turn one admin_call_log_list aaData row into a clean flat dict."""
    caller_html   = raw.get('4', '') or ''
    receiver_html = raw.get('5', '') or ''
    status_html   = raw.get('12', '') or ''
    actions_html  = raw.get('13', '') or ''

    m = re.search(r'data-customernumber="(\d*)"', caller_html)
    caller_number = m.group(1) if m else strip_tags(caller_html)

    recv_number = ''
    m = re.search(r'^(\d+)', strip_tags(receiver_html))
    if m:
        recv_number = m.group(1)
    recv_name = ''
    m = re.search(r'fa-user"></i>&nbsp([^<]*)', receiver_html)
    if m:
        recv_name = m.group(1).strip().rstrip('.').strip()

    m = re.search(r'data-recording="([^"]*)"', actions_html)
    recording = m.group(1) if m else ''

    direction = 'Inbound' if 'Inbound' in actions_html else ('Outbound' if 'Outbound' in actions_html else '')

    return {
        'sr_no':             raw.get('1'),
        'virtual_no':        raw.get('2'),
        'caller':            caller_number,
        'receiver_number':   recv_number,
        'receiver_name':     recv_name,
        'date':              raw.get('6'),
        'total_duration':    raw.get('7'),
        'answered_duration': raw.get('8'),
        'status':            strip_tags(status_html),
        'direction':         direction,
        'recording':         recording,
        'cdr_log_id':        raw.get('DT_RowId'),
    }


session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0'})

print('Fetching login page …')
r = session.get(LOGIN_URL, timeout=30)
csrf = extract_csrf(r.text)

print(f'Logging in as {USERNAME!r} …')
payload = {'username': USERNAME, 'password': PASSWORD}
if csrf:
    payload['csrfmiddlewaretoken'] = csrf
    session.headers.update({'Referer': LOGIN_URL})

r = session.post(LOGIN_URL, data=payload, timeout=30, allow_redirects=True)
if 'login' in r.url.lower():
    raise SystemExit('Login failed — still on login page. Check credentials.')
print(f'Logged in -> {r.url}')

r = session.get(CALL_LOG_PAGE, timeout=30)
csrf_token = extract_csrf_meta(r.text)
session.headers.update({
    'X-CSRF-TOKEN': csrf_token,
    'Referer': CALL_LOG_PAGE,
    'X-Requested-With': 'XMLHttpRequest',
})

r = session.get(PLAN_DETAILS_URL, timeout=30)
plans = r.json()['data']['getUserPlanDetails']
plan_by_number = {str(p['mapp_number']): p['user_plan_id'] for p in plans}
print(f'Account has {len(plan_by_number)} DID number(s): {sorted(plan_by_number)}')

target_numbers = args.did or sorted(plan_by_number)
missing = [n for n in target_numbers if n not in plan_by_number]
if missing:
    raise SystemExit(f'Unknown DID number(s) not on this account: {missing}')

PAGE_SIZE = 1000
results_by_number = {}

for number in target_numbers:
    plan_id = plan_by_number[number]
    print(f'\n=== {number} (plan_id={plan_id}) ===')
    r = session.get(CHANGE_PLAN_URL.format(plan_id), timeout=30)
    print(f'  Switched current plan -> status {r.status_code}')

    all_rows = []
    start = 0
    total_display = None
    while True:
        params = {
            'sEcho': 1,
            'iColumns': 14,
            'iDisplayStart': start,
            'iDisplayLength': PAGE_SIZE,
            'sSearch': '',
            'customer_number': '',
            'agent_number': '',
            'any_number': '',
            'Answered': 'false',
            'NoAnswered': 'false',
            'AFTHRS': 'false',
            'Ivr': 'false',
            'VoiceMail': 'false',
            'CdrLogArchive': 'false',
            'month_search': 1,          # "Month" quick-filter button
            'custom_search': 'Custom Range',
            'callType': '',
            'agent_name_filter': '',
            '_': int(time.time() * 1000),
        }
        r = session.get(CALL_LOG_URL, params=params, timeout=60)
        data = r.json()

        if total_display is None:
            total_display = int(data.get('iTotalDisplayRecords', 0))
            print(f'  iTotalRecords: {data.get("iTotalRecords")}  iTotalDisplayRecords: {total_display}')

        raw_rows = data.get('aaData', [])
        rows = [parse_row(rr) for rr in raw_rows]
        all_rows.extend(rows)
        if rows:
            print(f'  Fetched {len(rows)} rows (start={start}), total so far: {len(all_rows)}/{total_display}')

        start += PAGE_SIZE
        if len(raw_rows) < PAGE_SIZE or not total_display or len(all_rows) >= total_display:
            break

    results_by_number[number] = all_rows
    answered = sum(1 for row in all_rows if row['status'] == 'Answered')
    print(f'  -> {len(all_rows)} call(s) this month for {number}  ({answered} answered)')

print('\n=== Summary (this month) ===')
grand_total = 0
grand_answered = 0
for number in target_numbers:
    rows = results_by_number[number]
    answered = sum(1 for row in rows if row['status'] == 'Answered')
    grand_total += len(rows)
    grand_answered += answered
    print(f'  {number}: {len(rows)} calls  ({answered} answered)')
print(f'  TOTAL: {grand_total} calls  ({grand_answered} answered)')

out = 'greeter_ivr_month_by_number.json'
with open(out, 'w', encoding='utf-8') as f:
    json.dump(results_by_number, f, indent=2, ensure_ascii=False)
print(f'\nFull data saved -> {out}')

if args.csv:
    fieldnames = ['virtual_no', 'sr_no', 'date', 'caller', 'receiver_number', 'receiver_name',
                  'direction', 'status', 'total_duration', 'answered_duration', 'recording', 'cdr_log_id']
    with open(args.csv, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for number in target_numbers:
            for row in results_by_number[number]:
                writer.writerow(row)
    print(f'CSV saved -> {args.csv}')
