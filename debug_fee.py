#!/usr/bin/env python3
"""
Quick debug script: inspect Online MTD fee calculation.
Shows raw rows BEFORE dedup, AFTER dedup, and the per-counsellor sum.
Usage:  python debug_fee.py [YYYY-MM-DD]   (defaults to today IST)
"""
import asyncio, os, sys
from datetime import datetime, timedelta, UTC
from dotenv import load_dotenv
import asyncpg

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

DB = {
    "host":     os.getenv("ONLINE_LMS_DB_HOST"),
    "port":     int(os.getenv("ONLINE_LMS_DB_PORT", "54321")),
    "database": os.getenv("ONLINE_LMS_DB_NAME"),
    "user":     os.getenv("ONLINE_LMS_DB_USER"),
    "password": os.getenv("ONLINE_LMS_DB_PASSWORD"),
}

print("🔧 DB config loaded:")
for k, v in DB.items():
    if k == "password":
        print(f"   {k}: {'(set)' if v else '(MISSING)'}")
    else:
        print(f"   {k}: {v!r}")

missing = [k for k, v in DB.items() if not v]
if missing:
    print(f"\n❌ Missing env vars: {missing}")
    print("   Make sure you're running from the script directory, or the .env file exists at:")
    print(f"   {os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')}")
    sys.exit(1)

if len(sys.argv) > 1:
    report_date = datetime.strptime(sys.argv[1], '%Y-%m-%d')
else:
    now_ist = datetime.now(UTC) + timedelta(hours=5, minutes=30)
    report_date = now_ist

MTD_START = report_date.replace(day=1).strftime('%Y-%m-%d')
MTD_END   = report_date.strftime('%Y-%m-%d')
print(f"📅 MTD range: {MTD_START} → {MTD_END}\n")


async def main():
    conn = await asyncpg.connect(**DB)

    # ── 1. Raw rows (no dedup) ──────────────────────────────────────────────────
    raw = await conn.fetch(f"""
        SELECT
            csj.student_id,
            s.assigned_counsellor_id,
            c.counsellor_name,
            csj.deposit_amount,
            csj.fee_type,
            (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date AS adm_date
        FROM course_status_journeys csj
        JOIN students s ON s.student_id = csj.student_id
        LEFT JOIN counsellors c ON c.counsellor_id = s.assigned_counsellor_id
        WHERE csj.course_status = 'Admission'
          AND INITCAP(TRIM(csj.fee_type)) NOT IN ('Partial Paid', 'Partially Paid', 'Partial Done')
          AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date >= '{MTD_START}'::date
          AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date <= '{MTD_END}'::date
        ORDER BY csj.student_id, csj.deposit_amount DESC
    """)
    print(f"[1] Raw admission rows (no dedup): {len(raw)}")
    raw_total = sum(r['deposit_amount'] or 0 for r in raw)
    print(f"    Sum of all deposit_amount (raw): ₹{raw_total:,.0f}  ({raw_total/100000:.2f}L)\n")

    # Show students that appear more than once (duplicate entries)
    from collections import Counter
    dup_counts = Counter(r['student_id'] for r in raw)
    dups = {sid: cnt for sid, cnt in dup_counts.items() if cnt > 1}
    if dups:
        print(f"[!] Students with multiple admission rows ({len(dups)} students):")
        for sid, cnt in sorted(dups.items(), key=lambda x: -x[1])[:20]:
            student_rows = [r for r in raw if r['student_id'] == sid]
            amounts = [r['deposit_amount'] for r in student_rows]
            counsellor = student_rows[0]['counsellor_name']
            fee_types = [r['fee_type'] for r in student_rows]
            print(f"    student_id={sid}  counsellor={counsellor}  amounts={amounts}  fee_types={fee_types}")
    else:
        print("[✓] No duplicate student_id rows found.\n")

    # ── 2. After dedup (keep highest deposit per student) ──────────────────────
    deduped = await conn.fetch(f"""
        WITH deduped AS (
            SELECT DISTINCT ON (csj.student_id)
                s.assigned_counsellor_id,
                csj.student_id,
                csj.deposit_amount,
                csj.fee_type
            FROM course_status_journeys csj
            JOIN students s ON s.student_id = csj.student_id
            WHERE csj.course_status = 'Admission'
              AND INITCAP(TRIM(csj.fee_type)) NOT IN ('Partial Paid', 'Partially Paid', 'Partial Done')
              AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date >= '{MTD_START}'::date
              AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date <= '{MTD_END}'::date
            ORDER BY csj.student_id, csj.deposit_amount DESC
        )
        SELECT d.student_id, d.deposit_amount, d.fee_type, c.counsellor_name
        FROM deduped d
        LEFT JOIN counsellors c ON c.counsellor_id = d.assigned_counsellor_id
        ORDER BY d.deposit_amount DESC
    """)
    deduped_total = sum(r['deposit_amount'] or 0 for r in deduped)
    print(f"\n[2] After dedup (1 row per student, highest deposit): {len(deduped)} students")
    print(f"    Sum after dedup: ₹{deduped_total:,.0f}  ({deduped_total/100000:.2f}L)\n")

    # ── 3. Per-counsellor breakdown ─────────────────────────────────────────────
    per_couns = await conn.fetch(f"""
        WITH deduped AS (
            SELECT DISTINCT ON (csj.student_id)
                s.assigned_counsellor_id,
                csj.deposit_amount
            FROM course_status_journeys csj
            JOIN students s ON s.student_id = csj.student_id
            WHERE csj.course_status = 'Admission'
              AND INITCAP(TRIM(csj.fee_type)) NOT IN ('Partial Paid', 'Partially Paid', 'Partial Done')
              AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date >= '{MTD_START}'::date
              AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date <= '{MTD_END}'::date
            ORDER BY csj.student_id, csj.deposit_amount DESC
        )
        SELECT c.counsellor_name, COALESCE(SUM(d.deposit_amount), 0) AS mtd_fee, COUNT(*) AS students
        FROM deduped d
        LEFT JOIN counsellors c ON c.counsellor_id = d.assigned_counsellor_id
        GROUP BY c.counsellor_name
        ORDER BY mtd_fee DESC
    """)
    print("[3] Per-counsellor fee breakdown:")
    print(f"    {'Counsellor':<35} {'Students':>8}  {'MTD Fee':>14}")
    print(f"    {'-'*35} {'-'*8}  {'-'*14}")
    grand = 0
    for r in per_couns:
        name = r['counsellor_name'] or '(NULL / unassigned)'
        fee  = float(r['mtd_fee'])
        grand += fee
        print(f"    {name:<35} {r['students']:>8}  ₹{fee:>12,.0f}")
    print(f"    {'GRAND TOTAL':<35} {'':>8}  ₹{grand:>12,.0f}  ({grand/100000:.2f}L)")

    # ── 4. Check for NULL / zero deposit rows ───────────────────────────────────
    null_zero = await conn.fetch(f"""
        SELECT csj.student_id, csj.deposit_amount, csj.fee_type, c.counsellor_name
        FROM course_status_journeys csj
        JOIN students s ON s.student_id = csj.student_id
        LEFT JOIN counsellors c ON c.counsellor_id = s.assigned_counsellor_id
        WHERE csj.course_status = 'Admission'
          AND INITCAP(TRIM(csj.fee_type)) NOT IN ('Partial Paid', 'Partially Paid', 'Partial Done')
          AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date >= '{MTD_START}'::date
          AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date <= '{MTD_END}'::date
          AND (csj.deposit_amount IS NULL OR csj.deposit_amount = 0)
    """)
    if null_zero:
        print(f"\n[!] {len(null_zero)} admission rows with NULL or ₹0 deposit_amount:")
        for r in null_zero[:20]:
            print(f"    student_id={r['student_id']}  fee_type={r['fee_type']}  counsellor={r['counsellor_name']}")
    else:
        print(f"\n[✓] No NULL/zero deposit rows found.")

    # ── 5. Distinct fee_type values (sanity check on exclusion filter) ──────────
    fee_types = await conn.fetch(f"""
        SELECT DISTINCT INITCAP(TRIM(csj.fee_type)) AS ft, COUNT(*) AS cnt
        FROM course_status_journeys csj
        WHERE csj.course_status = 'Admission'
          AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date >= '{MTD_START}'::date
          AND (csj.created_at AT TIME ZONE 'Asia/Kolkata')::date <= '{MTD_END}'::date
        GROUP BY ft ORDER BY cnt DESC
    """)
    print(f"\n[4] Distinct fee_type values in this MTD window:")
    for r in fee_types:
        excluded = " ← EXCLUDED" if r['ft'] in ('Partial Paid', 'Partially Paid', 'Partial Done') else ""
        print(f"    {r['ft']:<30} ({r['cnt']} rows){excluded}")

    await conn.close()

asyncio.run(main())
