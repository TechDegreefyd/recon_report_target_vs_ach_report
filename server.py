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
from datetime import datetime, timedelta
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
# SCHEDULE  —  (hour, minute, script_filename, label, args_fn)  |  24h clock, IST
#                                              args_fn() → [date_str, cutoff_hour?]
# ═══════════════════════════════════════════════════════════════════════════════

def _yesterday_full():
    """10 AM: previous full day's recon data."""
    now_ist = datetime.now(IST)
    yesterday = now_ist - timedelta(days=1)
    return [yesterday.strftime('%Y-%m-%d')]

def _today_cutoff_12pm():
    """12 PM: today's recon data from midnight to 12 PM IST."""
    now_ist = datetime.now(IST)
    return [now_ist.strftime('%Y-%m-%d'), '12']

def _today_12pm_to_4pm():
    """4 PM: today's recon data from 12 PM to 4 PM IST."""
    now_ist = datetime.now(IST)
    return [now_ist.strftime('%Y-%m-%d'), '16', '12']


SCHEDULE = [
    (10, 0,  "generate_all_recon_reports.py",      "Recon — Yesterday (full day)",     _yesterday_full),
    (12, 0,  "generate_all_recon_reports.py",      "Recon — Today (until 12 PM)",      _today_cutoff_12pm),
    (16, 0,  "generate_all_recon_reports.py",      "Recon — Today (12 PM → 4 PM)",     _today_12pm_to_4pm),
    (20, 30, "generate_all_lms_reports.py",        "LMS Reports (Online + Regular)",   None),
]


def ist_now():
    return datetime.now(IST)


def run_script(script, label, args_fn=None):
    script_path = os.path.join(BASE_DIR, script)
    extra_args = args_fn() if args_fn else []
    cmd = [sys.executable, script_path] + extra_args
    start_mark = ist_now().strftime('%Y-%m-%d %H:%M:%S IST')

    print(f"\n{'─' * 70}", flush=True)
    print(f"[{start_mark}] ▶  START   — {label}", flush=True)
    print(f"   Python : {sys.executable}", flush=True)
    print(f"   Script : {script_path}", flush=True)
    print(f"   Args   : {extra_args}", flush=True)
    print(f"   CMD    : {' '.join(cmd)}", flush=True)
    print(f"   cwd    : {BASE_DIR}", flush=True)

    start_dt = ist_now()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=BASE_DIR,
            timeout=600,
        )
        elapsed = (ist_now() - start_dt).total_seconds()
        end_mark = ist_now().strftime('%Y-%m-%d %H:%M:%S IST')

        stdout_len = len(result.stdout) if result.stdout else 0
        stderr_len = len(result.stderr) if result.stderr else 0
        print(f"[{end_mark}] FINISH — elapsed={elapsed:.1f}s  code={result.returncode}  stdout={stdout_len}B  stderr={stderr_len}B", flush=True)

        if result.stdout.strip():
            print(f"═══ STDOUT ({stdout_len}B) ═══", flush=True)
            print(result.stdout.strip(), flush=True)
            print("═══ END STDOUT ═══", flush=True)
        if result.stderr.strip():
            print(f"═══ STDERR ({stderr_len}B) ═══", flush=True)
            print(result.stderr.strip(), flush=True)
            print("═══ END STDERR ═══", flush=True)

        if result.returncode == 0:
            print(f"[{end_mark}] ✓  DONE    — {label}", flush=True)
        else:
            print(f"[{end_mark}] ✗  FAIL    — {label}  (exit code={result.returncode})", flush=True)

    except subprocess.TimeoutExpired:
        print(f"[{ist_now().strftime('%Y-%m-%d %H:%M:%S IST')}] ⏰  TIMEOUT (600s) — {label}", flush=True)
    except Exception as e:
        import traceback
        print(f"[{ist_now().strftime('%Y-%m-%d %H:%M:%S IST')}] ✗  CRASH   — {label}: {e}", flush=True)
        traceback.print_exc()


def main():
    scheduler = BlockingScheduler(timezone=IST)

    for hour, minute, script, label, args_fn in SCHEDULE:
        scheduler.add_job(
            run_script,
            trigger=CronTrigger(hour=hour, minute=minute),
            args=[script, label, args_fn],
            id=label,
        )

    print("=" * 70, flush=True)
    print("  REPORT CRON SERVER — APScheduler (IST)", flush=True)
    print("=" * 70, flush=True)
    print(f"\n  Server time (IST): {ist_now().strftime('%Y-%m-%d %H:%M:%S %Z')}\n", flush=True)

    print("  SCHEDULE:", flush=True)
    for h, m, _, label, _ in SCHEDULE:
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
