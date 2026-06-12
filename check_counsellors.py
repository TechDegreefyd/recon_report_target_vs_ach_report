import asyncio
import asyncpg

async def main():
    conn = await asyncpg.connect(
        host='storage.bhugoal.cloud', port=54321,
        database='degreefyd_online_lms',
        user='mcp_read_user', password='DegreeFyd@9706'
    )

    # First check columns
    cols = await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name='counsellors' ORDER BY ordinal_position"
    )
    print("Columns:", [r['column_name'] for r in cols])

    names = [
        '%prashant mishra%',
        '%divya kawatra%',
        '%uzaif%',
        '%roshni%',
        '%swapnil chunar%',
    ]
    all_rows = []
    for n in names:
        rows = await conn.fetch(
            """
            SELECT c.*,
                   s.counsellor_name AS supervisor_name
            FROM counsellors c
            LEFT JOIN counsellors s ON s.counsellor_id = c.assigned_to
            WHERE c.counsellor_name ILIKE $1
            """,
            n
        )
        all_rows.extend(rows)
    await conn.close()

    print(f"\nFound {len(all_rows)} record(s)\n")
    for r in all_rows:
        print(dict(r))

asyncio.run(main())
