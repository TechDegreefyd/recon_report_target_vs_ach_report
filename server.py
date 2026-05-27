#!/usr/bin/env python3
"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REPORT CRON SERVER — IST Scheduler (APScheduler)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Long-running process that runs report scripts on schedule (Indian Standard Time).

  • LMS Reports (Online + Regular)  → generate_all_lms_reports.py
  • Recon Report                    → generate_all_recon_reports.py

Uses APScheduler with cron triggers — no manual loop, no duplicate-run tracking.
Usage:  python server.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import subprocess
import os
import sys
import glob as glob_mod
from datetime import datetime
from dotenv import load_dotenv

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

IST = pytz.timezone('Asia/Kolkata')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault('WORKSPACE_DIR', BASE_DIR)
load_dotenv(os.path.join(BASE_DIR, '.env'))

# ─── Docker secrets injection ────────────────────────────────────────────────
if os.getenv('GOOGLE_TOKEN_JSON'):
    with open(os.path.join(BASE_DIR, 'token.json'), 'w') as fh:
        fh.write(os.getenv('GOOGLE_TOKEN_JSON'))

if os.getenv('GOOGLE_CLIENT_SECRET_JSON'):
    existing = glob_mod.glob(os.path.join(BASE_DIR, 'client_secret_*.json'))
    target = existing[0] if existing else os.path.join(BASE_DIR, 'client_secret.json')
    with open(target, 'w') as fh:
        fh.write(os.getenv('GOOGLE_CLIENT_SECRET_JSON'))

# ═══════════════════════════════════════════════════════════════════════════════
# SCHEDULE  —  (hour, minute, script_filename, "label")  |  24h clock, IST
# ═══════════════════════════════════════════════════════════════════════════════

SCHEDULE = [
    (8,  30, "generate_all_lms_reports.py",     "LMS Reports (Online + Regular)"),
    (21,  30, "generate_all_recon_reports.py",    "Recon Report"),
]


def ist_now():
    return datetime.now(IST)


def run_script(script, label):
    script_path = os.path.join(BASE_DIR, script)
    start_mark = ist_now().strftime('%H:%M:%S IST')
    print(f"\n[{start_mark}] ▶  START  — {label}", flush=True)

    try:
        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            cwd=BASE_DIR,
            timeout=600,
        )
        stdout_tail = result.stdout.strip()[-600:]
        end_mark = ist_now().strftime('%H:%M:%S IST')

        if result.returncode == 0:
            print(f"[{end_mark}] ✓  DONE   — {label}", flush=True)
        else:
            stderr_tail = result.stderr.strip()[-400:]
            print(f"[{end_mark}] ✗  FAIL   — {label}  (code={result.returncode})", flush=True)
            if stderr_tail:
                print(f"  stderr: {stderr_tail}", flush=True)

        if stdout_tail:
            print(stdout_tail, flush=True)

    except subprocess.TimeoutExpired:
        print(f"[{ist_now().strftime('%H:%M:%S IST')}] ⏰ TIMEOUT — {label}", flush=True)
    except Exception as e:
        print(f"[{ist_now().strftime('%H:%M:%S IST')}] ✗  ERROR  — {label}: {e}", flush=True)


def main():
    scheduler = BlockingScheduler(timezone=IST)

    for hour, minute, script, label in SCHEDULE:
        scheduler.add_job(
            run_script,
            trigger=CronTrigger(hour=hour, minute=minute),
            args=[script, label],
            id=label,
        )

    print("=" * 70, flush=True)
    print("  REPORT CRON SERVER — APScheduler (IST)", flush=True)
    print("=" * 70, flush=True)
    print(f"\n  Server time (IST): {ist_now().strftime('%Y-%m-%d %H:%M:%S %Z')}\n", flush=True)

    print("  SCHEDULE:", flush=True)
    for h, m, _, label in SCHEDULE:
        print(f"    {h:02d}:{m:02d} IST  —  {label}", flush=True)

    print(f"\n{'─' * 70}", flush=True)
    print("  Scheduler started. Waiting for jobs...\n", flush=True)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("\n  Shutting down scheduler...", flush=True)
        scheduler.shutdown(wait=False)


if __name__ == '__main__':
    main()
