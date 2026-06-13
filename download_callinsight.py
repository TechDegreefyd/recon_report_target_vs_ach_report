#!/usr/bin/env python3
"""
Download call logs from CallInsight via API.
Usage:  python download_callinsight.py [--date DD/MM/YYYY]
  --date    date to download (default: today IST)
Saves filtered CSV to callinsight_downloads/.

Note: The CallInsight API daterange filter bleeds the next day's rows into
the response, so we filter client-side to keep only the requested date.
"""

import os
import sys
import csv
import io
import argparse
from datetime import datetime, timedelta, UTC
from dotenv import load_dotenv
import requests

_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_DIR, '.env'))

CI_EMAIL    = (os.getenv('CALLINSIGHT_EMAIL')    or '').strip()
CI_PASSWORD = (os.getenv('CALLINSIGHT_PASSWORD') or '').strip()
SAVE_DIR    = os.path.join(_DIR, 'callinsight_downloads')

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument('--date', default=None, help='DD/MM/YYYY (default: today IST)')
args, _ = parser.parse_known_args()


def parse_date_cell(v: str) -> str:
    import re
    v = v.strip().strip('"')
    m = re.match(r'^=?"?([0-9]{2}/[0-9]{2}/[0-9]{4})"?$', v)
    return m.group(1) if m else v


def download(target_date: str) -> str:
    if not CI_EMAIL or not CI_PASSWORD:
        raise RuntimeError('Set CALLINSIGHT_EMAIL and CALLINSIGHT_PASSWORD in .env')
    os.makedirs(SAVE_DIR, exist_ok=True)

    print(f'Logging into CallInsight API as {CI_EMAIL!r} ...')
    login_r = requests.post(
        'https://app.callinsight.io/api/login',
        json={'email': CI_EMAIL, 'password': CI_PASSWORD},
        timeout=30,
    )
    login_r.raise_for_status()
    token = login_r.json().get('data', {}).get('token', '')
    if not token:
        raise RuntimeError(f'Login failed: {login_r.text}')
    print('Logged in.')

    dt       = datetime.strptime(target_date, '%d/%m/%Y')
    date_iso = dt.strftime('%Y-%m-%d')

    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json', 'Accept': '*/*'}
    payload = {
        'sort': 'id', 'direction': 'desc', 'page': 0, 'per_page': 5000,
        'daterange': [f'{date_iso}T00:00:00.000Z', f'{date_iso}T23:59:59.000Z'],
        'call_status': '', 'call_type': '', 'organization': '', 'phone_numbers': [],
        'call_duration': [0, 7200], 'caller_number': '', 'is_archive': False,
        'filter_call_type': '', 'user': '', 'callback': '', 'department': '',
        'internal_numbers': 'Y', 'start_time': '00:00', 'end_time': '23:59',
        'is_initial_request': 1,
    }

    print(f'Downloading call logs for {target_date} ...')
    r = requests.post('https://app.callinsight.io/api/call-logs/download',
                      headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    raw_text = r.text

    # Client-side date filter — API bleeds next day's rows into the response
    reader = list(csv.DictReader(io.StringIO(raw_text)))
    kept   = [row for row in reader if parse_date_cell(row.get('Date', '')) == target_date]
    print(f'API returned {len(reader)} rows — kept {len(kept)} matching {target_date}')

    if not kept:
        save_path = os.path.join(SAVE_DIR, f'call_logs_{dt.strftime("%d%m%Y")}.csv')
        with open(save_path, 'w', encoding='utf-8') as f:
            f.write(raw_text)
        print(f'No rows matched — raw file saved → {save_path}')
        return save_path

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=reader[0].keys(), quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(kept)

    save_path = os.path.join(SAVE_DIR, f'call_logs_{dt.strftime("%d%m%Y")}.csv')
    with open(save_path, 'w', encoding='utf-8') as f:
        f.write(out.getvalue())
    print(f'Saved: {save_path}')
    return save_path


if __name__ == '__main__':
    if args.date:
        date_str = args.date
    else:
        now_ist  = datetime.now(UTC) + timedelta(hours=5, minutes=30)
        date_str = now_ist.strftime('%d/%m/%Y')

    path = download(date_str)
    print(f'\nDone. File: {path}')
