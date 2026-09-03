#!/usr/bin/env python3
"""
Marketing Hub Ingest — pulls Meta Ads + Google Ads spend/lead data for one day
and writes the rows straight into marketing_reports (degreefyd_marketing_hub).

Only ad-platform numbers (impressions, clicks, spend, panel_leads) are written.
lead_lms is not computed here — always written as 0.

No WhatsApp send — this just keeps the marketing_reports table current for the
CAG dashboard. Safe to re-run: existing rows for the same (date, channel) are
replaced, not duplicated.

Usage: python generate_marketing_hub_ingest.py [--date YYYY-MM-DD]
  --date   IST date to ingest (default: yesterday IST — ad platforms need a day
           to finalize spend/conversion numbers)
"""

import asyncio
import json
import os
import sys
import time
import uuid
import argparse
from datetime import datetime, timedelta, UTC

import requests
import asyncpg
from dotenv import load_dotenv

_START = time.perf_counter()


def log(msg: str):
    print(f'[{time.perf_counter() - _START:6.1f}s] {msg}', flush=True)


if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# ─── Args ────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(add_help=False)
parser.add_argument('--date', default=None, help='YYYY-MM-DD (default: yesterday IST)')
args, _ = parser.parse_known_args()

# ─── Paths / env ─────────────────────────────────────────────────────────────
_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_DIR, '.env'))

if args.date:
    TARGET_DATE = args.date
else:
    now_ist = datetime.now(UTC) + timedelta(hours=5, minutes=30)
    TARGET_DATE = (now_ist - timedelta(days=1)).strftime('%Y-%m-%d')

TARGET_DATE_OBJ = datetime.strptime(TARGET_DATE, '%Y-%m-%d').date()

META_TOKEN = os.getenv('META_ACCESS_TOKEN', '')

GOOGLE_ADS_DEV_TOKEN = os.getenv('GOOGLE_ADS_DEVELOPER_TOKEN', '')
GOOGLE_ADS_MCC_ID = os.getenv('GOOGLE_ADS_MCC_ID', '').replace('-', '')
GOOGLE_ADS_CLIENT_ID = os.getenv('GOOGLE_ADS_CLIENT_ID', '')
GOOGLE_ADS_CLIENT_SECRET = os.getenv('GOOGLE_ADS_CLIENT_SECRET', '')
GOOGLE_ADS_REFRESH_TOKEN = os.getenv('GOOGLE_ADS_REFRESH_TOKEN', '')
ADS_API_VERSION = 'v25'
ADS_BASE = f'https://googleads.googleapis.com/{ADS_API_VERSION}'

ACCOUNTS = {
    '2276414612586714': 'FaceBook_Degreefyd_B',
    '771369141855853':  'FaceBook_Degreefyd',
    '943943398169185':  'FaceBook_University_Admit',
    '2240320183392605': 'FaceBook_University_Admit_Video',
}

MARKETING_HUB_KWARGS = dict(
    host=os.getenv('MARKETING_HUB_DB_HOST'),
    port=int(os.getenv('MARKETING_HUB_DB_PORT', 5432)),
    database=os.getenv('MARKETING_HUB_DB_NAME'),
    user=os.getenv('MARKETING_HUB_DB_USER'),
    password=os.getenv('MARKETING_HUB_DB_PASSWORD'),
)


# ─── Meta Ads ──────────────────────────────────────────────────────────────
def fetch_meta_ads(date_str: str):
    all_meta = []
    for acc_id, acc_name in ACCOUNTS.items():
        url = f'https://graph.facebook.com/v19.0/act_{acc_id}/insights'
        params = {
            'level': 'ad',
            'fields': 'account_name,campaign_name,ad_name,spend,actions,date_start,impressions,clicks',
            'time_range': json.dumps({'since': date_str, 'until': date_str}),
            'time_increment': 1,
            'action_attribution_windows': json.dumps(['7d_click', '1d_view']),
            'access_token': META_TOKEN,
            'limit': 1000,
        }
        resp = requests.get(url, params=params, timeout=60).json()
        if 'error' in resp:
            log(f'  Meta[{acc_name}] ERROR: {resp["error"]}')
            continue
        for entry in resp.get('data', []):
            actions = entry.get('actions', [])
            action_map = {a['action_type']: int(a['value']) for a in actions}
            if 'onsite_conversion.lead_grouped' in action_map:
                leads = action_map['onsite_conversion.lead_grouped']
            elif 'offsite_conversion.fb_pixel_lead' in action_map:
                leads = action_map['offsite_conversion.fb_pixel_lead']
            else:
                leads = action_map.get('lead', 0)
            all_meta.append({
                'account': acc_name,
                'campaign': entry.get('campaign_name', ''),
                'ad_name': entry.get('ad_name', ''),
                'spend': float(entry.get('spend', 0)),
                'panel': leads,
                'impressions': int(entry.get('impressions', 0)),
                'clicks': int(entry.get('clicks', 0)),
            })
        log(f'  Meta[{acc_name}]: {len(resp.get("data", []))} ad rows')
    return all_meta


# ─── Google Ads ────────────────────────────────────────────────────────────
def google_ads_token():
    r = requests.post('https://oauth2.googleapis.com/token', data={
        'client_id': GOOGLE_ADS_CLIENT_ID,
        'client_secret': GOOGLE_ADS_CLIENT_SECRET,
        'refresh_token': GOOGLE_ADS_REFRESH_TOKEN,
        'grant_type': 'refresh_token',
    }, timeout=30)
    token = r.json().get('access_token')
    if not token:
        raise RuntimeError(f'Google Ads OAuth failed: {r.json()}')
    return token


def gaql(headers, customer_id, query):
    out = []
    body = {'query': query}
    while True:
        resp = requests.post(f'{ADS_BASE}/customers/{customer_id}/googleAds:search',
                              headers=headers, json=body, timeout=60)
        if resp.status_code != 200:
            log(f'  GoogleAds[{customer_id}] query failed ({resp.status_code}): {resp.text[:300]}')
            return out
        data = resp.json()
        out.extend(data.get('results', []))
        token_next = data.get('nextPageToken')
        if not token_next:
            break
        body['pageToken'] = token_next
    return out


def fetch_google_ads(date_str: str):
    token = google_ads_token()
    headers = {
        'Authorization': f'Bearer {token}',
        'developer-token': GOOGLE_ADS_DEV_TOKEN,
        'Content-Type': 'application/json',
        'login-customer-id': GOOGLE_ADS_MCC_ID,
    }
    client_rows = gaql(headers, GOOGLE_ADS_MCC_ID, """
        SELECT customer_client.id, customer_client.descriptive_name,
               customer_client.manager, customer_client.status
        FROM customer_client
        WHERE customer_client.manager = false AND customer_client.status = 'ENABLED'
    """)
    client_accounts = [(c['customerClient']['id'], c['customerClient']['descriptiveName']) for c in client_rows]
    log(f'  GoogleAds: {len(client_accounts)} enabled client accounts')

    campaign_query = f"""
        SELECT campaign.id, campaign.name, segments.date,
               metrics.cost_micros, metrics.conversions
        FROM campaign
        WHERE segments.date = '{date_str}'
    """
    all_rows = []
    for cid, account_name in client_accounts:
        rows = gaql(headers, cid, campaign_query)
        for row in rows:
            spend = int(row['metrics'].get('costMicros', 0)) / 1_000_000
            panel = float(row['metrics'].get('conversions', 0))
            all_rows.append({
                'account': account_name,
                'campaign': row['campaign']['name'],
                'spend': spend,
                'panel': panel,
            })
        if rows:
            log(f'  GoogleAds[{account_name}]: {len(rows)} campaign rows')
    return all_rows


# ─── DB write ──────────────────────────────────────────────────────────────
async def write_channel(conn, date_obj, channel, rows, row_key):
    """row_key(r) -> (account, campaign_name, ad_name, impressions, clicks, spend, panel)"""
    started = datetime.now(UTC)
    log_id = uuid.uuid4()
    async with conn.transaction():
        await conn.execute("""
            INSERT INTO processing_logs
                (id, file_name, file_path, file_size, total_rows, inserted_rows,
                 duplicate_rows, failed_rows, started_at, completed_at, status, created_at, updated_at)
            VALUES ($1, $2, $3, 0, $4, $4, 0, 0, $5, now(), 'completed', $5, now())
        """, log_id, f'cron-ingest-{channel.lower().replace(" ", "-")}-{date_obj}',
             'automation/generate_marketing_hub_ingest.py', len(rows), started)

        await conn.execute(
            "DELETE FROM marketing_reports WHERE date = $1 AND channel = $2",
            date_obj, channel,
        )

        for r in rows:
            account, campaign_name, ad_name, impressions, clicks, spend, panel = row_key(r)
            await conn.execute("""
                INSERT INTO marketing_reports
                    (id, import_log_id, date, channel, account, campaign_id, campaign_name, ad_name,
                     impressions, clicks, spend, panel_leads, lead_lms, created_at, updated_at)
                VALUES (gen_random_uuid(), $1, $2, $3, $4, NULL, $5, $6, $7, $8, $9, $10, 0, now(), now())
            """, log_id, date_obj, channel, account, campaign_name, ad_name,
                 impressions, clicks, spend, panel)
    log(f'  Wrote {len(rows)} "{channel}" rows for {date_obj} (log {log_id})')


# ─── Main ──────────────────────────────────────────────────────────────────
async def main():
    log(f'Marketing Hub ingest — target date {TARGET_DATE}')

    log('Fetching Meta Ads...')
    meta_rows = fetch_meta_ads(TARGET_DATE)
    log(f'  -> {len(meta_rows)} Meta ad rows')

    log('Fetching Google Ads...')
    google_rows = fetch_google_ads(TARGET_DATE)
    log(f'  -> {len(google_rows)} Google campaign rows')

    meta_panel = sum(r['panel'] for r in meta_rows)
    meta_spend = sum(r['spend'] for r in meta_rows)
    google_panel = sum(r['panel'] for r in google_rows)
    google_spend = sum(r['spend'] for r in google_rows)
    log(f'  Meta:   spend=Rs{meta_spend:.2f} panel={meta_panel}')
    log(f'  Google: spend=Rs{google_spend:.2f} panel={google_panel}')

    log('Writing to marketing_reports...')
    conn = await asyncpg.connect(**MARKETING_HUB_KWARGS, timeout=30)
    try:
        await write_channel(
            conn, TARGET_DATE_OBJ, 'Meta Ads', meta_rows,
            lambda r: (r['account'], r['campaign'], r['ad_name'], r['impressions'],
                       r['clicks'], round(r['spend'], 2), r['panel']),
        )
        await write_channel(
            conn, TARGET_DATE_OBJ, 'Google Ads', google_rows,
            lambda r: (r['account'], r['campaign'], None, 0,
                       0, round(r['spend'], 2), int(r['panel'])),
        )
    finally:
        await conn.close()

    log('Done.')


if __name__ == '__main__':
    asyncio.run(main())
