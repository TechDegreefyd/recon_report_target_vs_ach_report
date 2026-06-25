#!/usr/bin/env python3
"""
Quick diagnostic: how far behind is the CallInsight API right now?
Logs in, downloads today's calls, finds the most recent call time,
and prints the gap vs current IST time.

Usage:  python check_api_delay.py
"""

import os, csv, io, requests
from datetime import datetime, timedelta, UTC
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

IST_NOW = datetime.now(UTC) + timedelta(hours=5, minutes=30)
TODAY   = IST_NOW.strftime('%d/%m/%Y')
DATE_ISO = IST_NOW.strftime('%Y-%m-%d')

CI_EMAIL    = (os.getenv('CALLINSIGHT_EMAIL') or '').strip()
CI_PASSWORD = (os.getenv('CALLINSIGHT_PASSWORD') or '').strip()

print(f'Current IST time : {IST_NOW.strftime("%H:%M:%S")}')
print(f'Checking date    : {TODAY}')
print()

# Login
print('Logging in to CallInsight API...')
r = requests.post('https://app.callinsight.io/api/login',
                  json={'email': CI_EMAIL, 'password': CI_PASSWORD}, timeout=30)
r.raise_for_status()
token = r.json().get('data', {}).get('token', '')
print(f'Logged in. Token: {token[:12]}...')
print()

# Download
headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
payload = {
    'sort': 'id', 'direction': 'desc', 'page': 0, 'per_page': 5000,
    'daterange': [f'{DATE_ISO}T00:00:00.000Z', f'{DATE_ISO}T23:59:59.000Z'],
    'call_status': '', 'call_type': '', 'organization': '', 'phone_numbers': [],
    'call_duration': [0, 7200], 'caller_number': '', 'is_archive': False,
    'filter_call_type': '', 'user': '', 'callback': '', 'department': '',
    'internal_numbers': 'Y', 'start_time': '00:00', 'end_time': '23:59',
    'is_initial_request': 1,
}

print('Downloading call logs from API...')
fetch_start = datetime.now(UTC)
r = requests.post('https://app.callinsight.io/api/call-logs/download',
                  headers=headers, json=payload, timeout=60)
fetch_secs = (datetime.now(UTC) - fetch_start).total_seconds()
r.raise_for_status()

rows = list(csv.DictReader(io.StringIO(r.text)))
print(f'API returned {len(rows)} total rows  (fetch took {fetch_secs:.1f}s)')
print()

# Find most recent call time
times = []
for row in rows:
    t = row.get('Time', '').strip()[:5]
    if len(t) == 5:
        times.append(t)

if not times:
    print('No calls found in API response for today.')
else:
    latest_hhmm = max(times)
    lh, lm = int(latest_hhmm[:2]), int(latest_hhmm[3:])
    latest_dt = IST_NOW.replace(hour=lh, minute=lm, second=0, microsecond=0)
    delay_secs = (IST_NOW - latest_dt).total_seconds()
    delay_min  = int(delay_secs // 60)
    delay_s    = int(delay_secs % 60)

    print(f'Most recent call in API : {latest_hhmm} IST')
    print(f'Current IST time        : {IST_NOW.strftime("%H:%M")} IST')
    print()
    if delay_secs < 0:
        print('API is UP TO DATE (no delay detected)')
    else:
        print(f'>>> API SYNC DELAY : {delay_min}m {delay_s:02d}s <<<')
        if delay_min < 5:
            print('    Delay is SMALL — API is nearly real-time.')
        elif delay_min < 20:
            print('    Delay is MODERATE — first 10:00 AM report may miss recent calls.')
        else:
            print('    Delay is LARGE — early reports will be significantly incomplete.')

    # Show per-SIM breakdown of latest call time
    print()
    print('Latest call time per SIM (top 10 most active):')
    sim_latest = {}
    for row in rows:
        sim = row.get('SIM Number', '').strip().split()[0]
        t   = row.get('Time', '').strip()[:5]
        if sim and len(t) == 5:
            if sim not in sim_latest or t > sim_latest[sim]:
                sim_latest[sim] = t

    for sim, t in sorted(sim_latest.items(), key=lambda x: -int(x[1].replace(':', ''))):
        lh2, lm2 = int(t[:2]), int(t[3:])
        dt2 = IST_NOW.replace(hour=lh2, minute=lm2, second=0, microsecond=0)
        gap = int((IST_NOW - dt2).total_seconds() // 60)
        print(f'  SIM {sim}  last call {t}  ({gap}m ago)')
        if sim_latest and list(sim_latest.keys()).index(sim) >= 9:
            break
