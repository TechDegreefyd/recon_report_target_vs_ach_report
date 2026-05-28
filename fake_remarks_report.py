#!/usr/bin/env python3
"""
FAKE REMARKS DETECTION REPORT — All 4 LMS Databases
Queries all 4 databases for RNR/ringing/not-reachable remarks, then flags
fake remarks using three buckets:
  1. Gap <= 30 sec   — Too fast between different students (click spam)
  2. Gap 30-180 sec + both prev & current calling_status = 'Not Connected'
     + different students — Counsellor went from one dead call to another
     with no real activity in between (SQL-filtered, most precise)
  3. Gap > 3 min     — Two consecutive RNR remarks with long gap, different students

Output: 4 Excel files (one per DB), each with 4 sheets
(Gap_Under_30sec, Gap_30-180s_NotConnected, Gap_Over_3min, Pivot_Counsellor).

Usage:  python fake_remarks_report.py 2026-05-28
"""

import asyncio
import os
import sys

import asyncpg
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))

if len(sys.argv) < 2:
    print("ERROR: Date required. Usage: python fake_remarks_report.py 2026-05-28")
    sys.exit(1)

REPORT_DATE = sys.argv[1]

OUTPUT_DIR = os.path.join(BASE_DIR, 'Automation Cron Job', 'Fake Remarks')
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ─── 4 Database configs ──────────────────────────────────────────────────────

DB_CONFIGS = [
    {
        "label": "Online",
        "host": os.getenv("ONLINE_LMS_DB_HOST"),
        "port": int(os.getenv("ONLINE_LMS_DB_PORT", "54321")),
        "database": os.getenv("ONLINE_LMS_DB_NAME"),
        "user": os.getenv("ONLINE_LMS_DB_USER"),
        "password": os.getenv("ONLINE_LMS_DB_PASSWORD"),
    },
    {
        "label": "Regular",
        "host": os.getenv("REGULAR_LMS_DB_HOST"),
        "port": int(os.getenv("REGULAR_LMS_DB_PORT", "54321")),
        "database": os.getenv("REGULAR_LMS_DB_NAME"),
        "user": os.getenv("REGULAR_LMS_DB_USER"),
        "password": os.getenv("REGULAR_LMS_DB_PASSWORD"),
    },
    {
        "label": "CGC",
        "host": os.getenv("REGULAR_CGC_LMS_DB_HOST"),
        "port": int(os.getenv("REGULAR_CGC_LMS_DB_PORT", "54321")),
        "database": os.getenv("REGULAR_CGC_LMS_DB_NAME"),
        "user": os.getenv("REGULAR_CGC_LMS_DB_USER"),
        "password": os.getenv("REGULAR_CGC_LMS_DB_PASSWORD"),
    },
    {
        "label": "Amity",
        "host": os.getenv("REGULAR_AMITY_LMS_DB_HOST"),
        "port": int(os.getenv("REGULAR_AMITY_LMS_DB_PORT", "54321")),
        "database": os.getenv("REGULAR_AMITY_LMS_DB_NAME"),
        "user": os.getenv("REGULAR_AMITY_LMS_DB_USER"),
        "password": os.getenv("REGULAR_AMITY_LMS_DB_PASSWORD"),
    },
]

# ─── SQL: Broad query — all RNR remarks with gap + prev data ─────────────────

_ALL_RNR_SQL = r"""
WITH all_data AS (
    SELECT
        c.counsellor_name,
        s.student_id,
        s.student_name,
        s.created_at AT TIME ZONE 'Asia/Kolkata' AS student_created_at_ist,
        lal.created_at AT TIME ZONE 'Asia/Kolkata' AS lead_assigned_at_ist,
        sr.remark_id,
        sr.calling_status,
        sr.sub_calling_status,
        sr.remarks,
        sr.created_at AT TIME ZONE 'Asia/Kolkata' AS remark_time_ist,

        LAG(sr.created_at AT TIME ZONE 'Asia/Kolkata') OVER (
            PARTITION BY sr.counsellor_id ORDER BY sr.created_at
        ) AS previous_remark_time,

        LAG(sr.calling_status) OVER (
            PARTITION BY sr.counsellor_id ORDER BY sr.created_at
        ) AS prev_calling_status,

        LAG(sr.student_id) OVER (
            PARTITION BY sr.counsellor_id ORDER BY sr.created_at
        ) AS prev_student_id,

        ROUND(
            EXTRACT(EPOCH FROM (
                sr.created_at - LAG(sr.created_at) OVER (
                    PARTITION BY sr.counsellor_id ORDER BY sr.created_at
                )
            )) / 60.0,
            2
        ) AS gap_from_prev_remark_minutes,

        EXTRACT(EPOCH FROM (
            sr.created_at - LAG(sr.created_at) OVER (
                PARTITION BY sr.counsellor_id ORDER BY sr.created_at
            )
        )) AS gap_seconds,

        sr.callback_date

    FROM student_remarks sr
    JOIN counsellors c ON sr.counsellor_id = c.counsellor_id
    JOIN students s ON sr.student_id = s.student_id

    LEFT JOIN LATERAL (
        SELECT created_at
        FROM lead_assignment_log
        WHERE student_id = sr.student_id
          AND assigned_counsellor_id = sr.counsellor_id
        ORDER BY created_at ASC LIMIT 1
    ) lal ON true

    WHERE (sr.created_at AT TIME ZONE 'Asia/Kolkata')::date = $1::date
)
SELECT * FROM all_data
WHERE LOWER(TRIM(remarks)) ~
    '^(rnr|r\.n\.r|ring|ringing|rng|ringg|ringng|not reachable|nr|no response|not responding|switched off|busy|no answer|call back|later|not picking|not received|not attended|out of coverage|network issue)'
ORDER BY counsellor_name, remark_time_ist ASC;
"""

# ─── SQL: Precise query — NotConnected→NotConnected, gap 30-180s, different students ──

_NOTCONNECTED_SQL = r"""
WITH all_data AS (
    SELECT
        c.counsellor_name,
        s.student_id,
        s.student_name,
        s.created_at AT TIME ZONE 'Asia/Kolkata' AS student_created_at_ist,
        lal.created_at AT TIME ZONE 'Asia/Kolkata' AS lead_assigned_at_ist,
        sr.remark_id,
        sr.calling_status,
        sr.sub_calling_status,
        sr.remarks,
        sr.created_at AT TIME ZONE 'Asia/Kolkata' AS remark_time_ist,

        LAG(sr.created_at AT TIME ZONE 'Asia/Kolkata') OVER (
            PARTITION BY sr.counsellor_id ORDER BY sr.created_at
        ) AS previous_remark_time,

        LAG(sr.calling_status) OVER (
            PARTITION BY sr.counsellor_id ORDER BY sr.created_at
        ) AS prev_calling_status,

        LAG(sr.student_id) OVER (
            PARTITION BY sr.counsellor_id ORDER BY sr.created_at
        ) AS prev_student_id,

        ROUND(
            EXTRACT(EPOCH FROM (
                sr.created_at - LAG(sr.created_at) OVER (
                    PARTITION BY sr.counsellor_id ORDER BY sr.created_at
                )
            )) / 60.0,
            2
        ) AS gap_from_prev_remark_minutes,

        EXTRACT(EPOCH FROM (
            sr.created_at - LAG(sr.created_at) OVER (
                PARTITION BY sr.counsellor_id ORDER BY sr.created_at
            )
        )) AS gap_seconds,

        sr.callback_date

    FROM student_remarks sr
    JOIN counsellors c ON sr.counsellor_id = c.counsellor_id
    JOIN students s ON sr.student_id = s.student_id

    LEFT JOIN LATERAL (
        SELECT created_at
        FROM lead_assignment_log
        WHERE student_id = sr.student_id
          AND assigned_counsellor_id = sr.counsellor_id
        ORDER BY created_at ASC LIMIT 1
    ) lal ON true

    WHERE (sr.created_at AT TIME ZONE 'Asia/Kolkata')::date = $1::date
)
SELECT * FROM all_data
WHERE LOWER(TRIM(remarks)) ~
    '^(rnr|r\.n\.r|ring|ringing|rng|ringg|ringng|not reachable|nr|no response|not responding|switched off|busy|no answer|call back|later|not picking|not received|not attended|out of coverage|network issue)'
  AND gap_seconds IS NOT NULL
  AND gap_seconds >= 30
  AND gap_seconds < 180
  AND calling_status = 'Not Connected'
  AND prev_calling_status = 'Not Connected'
  AND prev_student_id != student_id
ORDER BY counsellor_name, remark_time_ist ASC;
"""

# ─── RNR pattern for bucket-3 detection ──────────────────────────────────────

_RNR_PATTERN = (
    r'^(rnr|r\.n\.r|ring|ringing|rng|ringg|ringng|not reachable|nr'
    r'|no response|not responding|switched off|busy|no answer'
    r'|call back|later|not picking|not received|not attended'
    r'|out of coverage|network issue)'
)


async def fetch_broad_remarks(db_config):
    """Fetch ALL RNR remarks with gap data (used for buckets 1 and 3)."""
    conn = await asyncpg.connect(
        host=db_config["host"], port=db_config["port"],
        database=db_config["database"],
        user=db_config["user"], password=db_config["password"],
    )
    try:
        rows = await conn.fetch(_ALL_RNR_SQL, datetime.strptime(REPORT_DATE, '%Y-%m-%d').date())
    finally:
        await conn.close()
    return db_config["label"], pd.DataFrame([dict(r) for r in rows])


async def fetch_notconnected_remarks(db_config):
    """Fetch ONLY NotConnected→NotConnected, gap 30-180s, different students."""
    conn = await asyncpg.connect(
        host=db_config["host"], port=db_config["port"],
        database=db_config["database"],
        user=db_config["user"], password=db_config["password"],
    )
    try:
        rows = await conn.fetch(_NOTCONNECTED_SQL, datetime.strptime(REPORT_DATE, '%Y-%m-%d').date())
    finally:
        await conn.close()
    return db_config["label"], pd.DataFrame([dict(r) for r in rows])


def build_report(df_all, df_nc, label):
    """df_all = broad query results, df_nc = not-connected query results."""

    if df_all.empty:
        print(f"  [{label}] No RNR remarks found for {REPORT_DATE}")
        return None, None, None, None

    df = df_all.copy()

    # ── Bucket 1: gap <= 30 sec, different students ──
    bucket_1 = df[
        df['gap_seconds'].notna()
        & (df['gap_seconds'] <= 30)
        & (df['student_id'] != df['prev_student_id'])
    ].copy()

    # ── Bucket 2: from the precise SQL query (already filtered) ──
    bucket_2 = df_nc.copy() if not df_nc.empty else pd.DataFrame()

    # ── Bucket 3: gap > 3 min, both RNR-type, different students ──
    df['remarks_lower'] = df['remarks'].astype(str).str.strip().str.lower()

    is_rnr = df['remarks_lower'].str.match(_RNR_PATTERN, na=False)
    prev_is_rnr = df.groupby('counsellor_name')['remarks_lower'].shift(1).str.match(_RNR_PATTERN, na=False)

    bucket_3 = df[
        df['gap_seconds'].notna()
        & (df['gap_seconds'] > 180)
        & is_rnr
        & prev_is_rnr
        & (df['student_id'] != df['prev_student_id'])
    ].copy()

    # Drop helper columns
    for col in ['remarks_lower']:
        if col in df.columns:
            df.drop(columns=[col], inplace=True)
        for b in [bucket_1, bucket_2, bucket_3]:
            if col in b.columns:
                b.drop(columns=[col], inplace=True)

    # ── Pivot: per-counsellor sums ──
    pivot_rows = []
    all_counsellors = set()
    for b in [bucket_1, bucket_2, bucket_3]:
        if not b.empty:
            all_counsellors.update(b['counsellor_name'].unique())

    for name in sorted(all_counsellors):
        b1 = bucket_1[bucket_1['counsellor_name'] == name] if not bucket_1.empty else pd.DataFrame()
        b2 = bucket_2[bucket_2['counsellor_name'] == name] if not bucket_2.empty else pd.DataFrame()
        b3 = bucket_3[bucket_3['counsellor_name'] == name] if not bucket_3.empty else pd.DataFrame()
        pivot_rows.append({
            'counsellor_name': name,
            'Count_<=30sec': len(b1),
            'Sum_Gap_<=30sec': round(b1['gap_seconds'].sum(), 2) if not b1.empty else 0,
            'Count_30-180s_NotConnected': len(b2),
            'Sum_Gap_30-180s_NotConnected': round(b2['gap_seconds'].sum(), 2) if not b2.empty else 0,
            'Count_>3min_RNR_to_RNR': len(b3),
            'Sum_Gap_>3min_RNR_to_RNR': round(b3['gap_seconds'].sum(), 2) if not b3.empty else 0,
        })

    pivot_df = pd.DataFrame(pivot_rows)

    print(f"  [{label}] RNR remarks: {len(df)} | <=30s: {len(bucket_1)} | 30-180s NotConnected: {len(bucket_2)} | >3min: {len(bucket_3)}")
    return bucket_1, bucket_2, bucket_3, pivot_df


def write_excel(bucket_1, bucket_2, bucket_3, pivot_df, label):
    filepath = os.path.join(OUTPUT_DIR, f'{label}_Fake_Remarks_{REPORT_DATE}.xlsx')

    with pd.ExcelWriter(filepath, engine='openpyxl') as writer:
        if bucket_1 is not None and not bucket_1.empty:
            bucket_1.sort_values(['counsellor_name', 'remark_time_ist']).to_excel(
                writer, sheet_name='Gap_Under_30sec', index=False
            )
        else:
            pd.DataFrame({'message': ['No remarks with gap <= 30 sec']}).to_excel(
                writer, sheet_name='Gap_Under_30sec', index=False
            )

        if bucket_2 is not None and not bucket_2.empty:
            bucket_2.sort_values(['counsellor_name', 'remark_time_ist']).to_excel(
                writer, sheet_name='Gap_30-180s_NotConnected', index=False
            )
        else:
            pd.DataFrame({'message': ['No remarks meeting NotConnected->NotConnected, 30-180s, diff students']}).to_excel(
                writer, sheet_name='Gap_30-180s_NotConnected', index=False
            )

        if bucket_3 is not None and not bucket_3.empty:
            bucket_3.sort_values(['counsellor_name', 'remark_time_ist']).to_excel(
                writer, sheet_name='Gap_Over_3min', index=False
            )
        else:
            pd.DataFrame({'message': ['No remarks with gap > 3 min (RNR->RNR)']}).to_excel(
                writer, sheet_name='Gap_Over_3min', index=False
            )

        if pivot_df is not None and not pivot_df.empty:
            pivot_df.to_excel(writer, sheet_name='Pivot_Counsellor', index=False)
        else:
            pd.DataFrame({'message': ['No data for pivot']}).to_excel(
                writer, sheet_name='Pivot_Counsellor', index=False
            )

    print(f"  [{label}] Saved: {filepath}")
    return filepath


async def main():
    print("=" * 70)
    print("  FAKE REMARKS DETECTION REPORT")
    print(f"  Date: {REPORT_DATE}")
    print("=" * 70)

    # STEP 1: Fetch from all 4 DBs concurrently (both queries per DB)
    print("\n--- Fetching from all 4 databases concurrently...")
    broad_tasks = [fetch_broad_remarks(cfg) for cfg in DB_CONFIGS]
    nc_tasks = [fetch_notconnected_remarks(cfg) for cfg in DB_CONFIGS]

    broad_results = await asyncio.gather(*broad_tasks)
    nc_results = await asyncio.gather(*nc_tasks)

    # STEP 2: Process each DB's data
    print("\n--- Processing results...")
    for (label_all, df_all), (label_nc, df_nc) in zip(broad_results, nc_results):
        print(f"  [{label_all}] Fetched broad={len(df_all)} rows, NotConnected={len(df_nc)} rows")
        b1, b2, b3, pivot = build_report(df_all, df_nc, label_all)
        write_excel(b1, b2, b3, pivot, label_all)

    print("\n" + "=" * 70)
    print(f"  DONE - 4 Excel files saved in: {OUTPUT_DIR}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
