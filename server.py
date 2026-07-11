#!/usr/bin/env python3
"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REPORT CRON SERVER — UTC Scheduler (APScheduler)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Long-running process that runs report scripts on schedule (UTC times, IST-equivalent).

  • LMS Reports (Online + Regular)  → generate_all_lms_reports.py
  • Recon Report                    → generate_all_recon_reports.py
  • Bhugoal Report                  → bhugoal_generate_report.py
  • Callback Reports (Today+Overdue)→ generate_callback_reports.py

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
# SCHEDULE  —  (hour, minute, script_filename, label, args_fn)  |  24h clock, UTC
#                                              args_fn() → [date_str, cutoff_hour?]
# ═══════════════════════════════════════════════════════════════════════════════

def _yesterday_full():
    """10 AM IST (4:30 UTC): previous full day's recon data."""
    now_ist = datetime.now(IST)
    yesterday = now_ist - timedelta(days=1)
    return [yesterday.strftime('%Y-%m-%d')]

def _today_cutoff_10am():
    """10 AM IST (4:30 UTC): today's recon data from midnight to 10 AM IST."""
    now_ist = datetime.now(IST)
    return [now_ist.strftime('%Y-%m-%d'), '10']

def _today_cutoff_12pm():
    """12 PM IST (6:30 UTC): today's recon data from midnight to 12 PM IST."""
    now_ist = datetime.now(IST)
    return [now_ist.strftime('%Y-%m-%d'), '12']

def _today_midnight_to_4pm():
    """4 PM IST (10:30 UTC): today's cumulative recon data from midnight to 4 PM IST."""
    now_ist = datetime.now(IST)
    return [now_ist.strftime('%Y-%m-%d'), '16']

def _today_midnight_to_7pm():
    """7 PM IST (13:30 UTC): today's cumulative recon data from midnight to 7 PM IST."""
    now_ist = datetime.now(IST)
    return [now_ist.strftime('%Y-%m-%d'), '19']

def _lms_yesterday():
    """10 AM IST (4:30 UTC): Last Activity report only, for previous day (IST)."""
    return ['--yesterday', '--last-activity-only']


# ─── Call Report helpers ──────────────────────────────────────────────────────
_CALL_GROUP    = os.getenv('WHATSAPP_GROUP_ONLINE_LOB', '120363424062745706@g.us')
_GREETER_GROUP = os.getenv('WHATSAPP_GROUP_GREETER',    '120363426619711887@g.us')

def _call_cumulative_ob():
    """Outbound core only: shift start (9:15 AM) → now minus 15m API sync buffer."""
    now_ist  = datetime.now(IST)
    to_dt    = now_ist - timedelta(minutes=15)
    return ['--from-time', '09:15', '--to-time', to_dt.strftime('%H:%M'),
            '--group', _CALL_GROUP, '--only-core']

def _call_last_hour_ob():
    """Outbound core only: last 1-hour window minus 15m API sync buffer (cadence is now hourly)."""
    now_ist  = datetime.now(IST)
    to_dt    = now_ist - timedelta(minutes=15)
    from_dt  = to_dt   - timedelta(hours=1)
    return ['--from-time', from_dt.strftime('%H:%M'), '--to-time', to_dt.strftime('%H:%M'),
            '--group', _CALL_GROUP, '--only-core']

def _call_ob_eod_today():
    """Online outbound core only: today's full day data (sent at 11:30 PM IST same day = UTC 18:00)."""
    now_ist = datetime.now(IST)
    today = now_ist.strftime('%d/%m/%Y')
    return ['--date', today, '--group', _CALL_GROUP, '--only-core', '--eod']

def _call_cumulative_ib():
    """Inbound: shift start (9:30 AM) → now."""
    now_ist = datetime.now(IST)
    return ['--from-time', '09:30', '--to-time', now_ist.strftime('%H:%M'),
            '--group', _CALL_GROUP]

def _call_last_hour_ib():
    """Inbound: last 2-hour window (schedule fires every 2h)."""
    now_ist  = datetime.now(IST)
    from_dt  = now_ist - timedelta(hours=2)
    return ['--from-time', from_dt.strftime('%H:%M'), '--to-time', now_ist.strftime('%H:%M'),
            '--group', _CALL_GROUP]

def _greeter_cumulative():
    """Greeter: shift start (9:30 AM) → now minus 15m API sync buffer."""
    now_ist = datetime.now(IST)
    to_dt   = now_ist - timedelta(minutes=15)
    return ['--from-time', '09:30', '--to-time', to_dt.strftime('%H:%M'),
            '--group', _GREETER_GROUP]

def _greeter_last_2hr():
    """Greeter: last 2-hour window minus 15m API sync buffer."""
    now_ist = datetime.now(IST)
    to_dt   = now_ist - timedelta(minutes=15)
    from_dt = to_dt   - timedelta(hours=2)
    return ['--from-time', from_dt.strftime('%H:%M'), '--to-time', to_dt.strftime('%H:%M'),
            '--group', _GREETER_GROUP]

# ─── Regular outbound helpers ─────────────────────────────────────────────────
def _regular_ob_eod_today():
    """Regular outbound: today's full day data (sent at 11:30 PM IST same day = UTC 18:00)."""
    now_ist = datetime.now(IST)
    today = now_ist.strftime('%d/%m/%Y')
    return ['--date', today, '--only-regular', '--eod']

def _regular_ob_cumulative():
    """Regular outbound: shift start (9:15 AM) → now minus 15m API sync buffer."""
    now_ist = datetime.now(IST)
    to_dt   = now_ist - timedelta(minutes=15)
    return ['--from-time', '09:15', '--to-time', to_dt.strftime('%H:%M'), '--only-regular']

# ─── Greeter helpers (new schedule: 11:30 PM same-day EOD, then 2-hr cumulative) ───────
def _greeter_eod_today():
    """Greeter: today's full day data (sent at 11:30 PM IST same day = UTC 18:00)."""
    return ['--date', 'today', '--group', _GREETER_GROUP]


# Online LOB (core) outbound: hourly cadence, 9:15 AM – 9 PM IST
_CALL_HOURS_UTC = list(range(4, 16, 1))   # 4,5,6,...,15 → hourly slots (real IST times land on the hour, e.g. 11:00,12:00...21:00)

# IST 9:30 AM – 7:30 PM = UTC 04:00 – 14:00  →  new schedule for regular + greeter
_NEW_HOURS_UTC = list(range(4, 15, 2))    # 4,6,8,10,12,14 → IST 9:30,11:30,13:30,15:30,17:30,19:30

SCHEDULE = [
    (4,  30, "generate_all_recon_reports.py",  "Recon — Yesterday (full day)",               _yesterday_full),
    (4,  35, "generate_all_recon_reports.py",  "Recon — Today (midnight → 10 AM)",           _today_cutoff_10am),
    (4,  30, "generate_all_lms_reports.py",    "LMS Reports — Morning (yesterday IST)",      _lms_yesterday),
    (5,  35, "tat_reports.py",                "TAT Reports — Online LOB (11:05 AM IST)",    None),
    (6,  30, "generate_all_recon_reports.py",  "Recon — Today (midnight → 12 PM)",           _today_cutoff_12pm),
    (10, 30, "generate_all_recon_reports.py",  "Recon — Today (midnight → 4 PM cumulative)", _today_midnight_to_4pm),
    (13, 30, "generate_all_recon_reports.py",  "Recon — Today (midnight → 7 PM cumulative)", _today_midnight_to_7pm),
    (15, 0,  "generate_all_lms_reports.py",    "LMS Reports (Online + Regular)",             lambda: ['--skip-last-activity']),
    (4,  40, "bhugoal_generate_report.py",     "Bhugoal Report — 10:00 AM IST (yesterday)",  None),
]

# ─── Call Report schedule (online LOB / core, hourly) ─────────────────────────
# Inbound reports stopped. Outbound only: cumulative + last-1hr window.
# First slot fires at true 9:15 IST = UTC 03:45 (IST = UTC + 5:30), then hourly
# from the second grid slot (UTC hour 5 = real IST 11:00) onward.
CALL_SCHEDULE = []
for _i, _utc_h in enumerate(_CALL_HOURS_UTC):
    _ist_h = (_utc_h + 5) % 24
    if _i == 0:  # first slot: true 9:15 IST (UTC 03:45), not UTC 04:15 (real 9:45)
        CALL_SCHEDULE += [
            (3, 45, "generate_outbound_report.py",
             "Outbound Cumulative — 09:15 IST", _call_cumulative_ob),
            (3, 47, "generate_outbound_report.py",
             "Outbound Last Hour  — 09:15 IST", _call_last_hour_ob),
        ]
    else:
        CALL_SCHEDULE += [
            (_utc_h, 30, "generate_outbound_report.py",
             f"Outbound Cumulative — {_ist_h:02d}:30 IST", _call_cumulative_ob),
            (_utc_h, 32, "generate_outbound_report.py",
             f"Outbound Last Hour  — {_ist_h:02d}:30 IST", _call_last_hour_ob),
        ]

# Custom slot: 7:30 PM IST (UTC 14:00) — the hourly grid above already lands on
# 7:00 PM (UTC 13:30) and 8:00 PM (UTC 14:30), so only 7:30 PM needs adding here.
# Minutes 4/6 keep it clear of the 8 PM grid slot (14:30/14:32) and of the
# Regular Outbound / Greeter jobs that also fire at UTC 14:00/14:02.
CALL_SCHEDULE += [
    (14, 4, "generate_outbound_report.py",
     "Outbound Cumulative — 19:30 IST (custom)", _call_cumulative_ob),
    (14, 6, "generate_outbound_report.py",
     "Outbound Last Hour  — 19:30 IST (custom)", _call_last_hour_ob),
]

# UTC 18:00 = IST 23:30 → EOD full day report (Online outbound, core only)
CALL_SCHEDULE.append(
    (18, 1, "generate_outbound_report.py",
     "Online Outbound EOD Today — 23:30 IST", _call_ob_eod_today)
)

# ─── Regular outbound + Greeter schedule ─────────────────────────────────────
# Regular outbound: 9:30 AM IST → yesterday full day, then every 2h cumulative
# Greeter:          11:30, 13:30, 15:30, 17:30, 19:30 IST → cumulative today
#                   23:30 IST (UTC 18:00) → today's full day EOD report
REGULAR_OUTBOUND_SCHEDULE = [
    # first slot: true 9:15 IST (UTC 03:49) — matches core's shift-start time
    (3, 49, "generate_outbound_report.py",
     "Regular Outbound Cumulative — 09:15 IST", _regular_ob_cumulative),
]
GREETER_SCHEDULE = []
for _i, _utc_h in enumerate(_NEW_HOURS_UTC):
    _ist_h = (_utc_h + 5) % 24
    _ist_m = 30
    if _i == 0:  # 9:15 AM slot handled above, outside the loop
        pass
    else:  # 11:30, 13:30, 15:30, 17:30, 19:30 IST → cumulative today
        REGULAR_OUTBOUND_SCHEDULE.append(
            (_utc_h, 0, "generate_outbound_report.py",
             f"Regular Outbound Cumulative — {_ist_h:02d}:{_ist_m:02d} IST", _regular_ob_cumulative)
        )
        GREETER_SCHEDULE.append(
            (_utc_h, 2, "generate_greeter_report.py",
             f"Greeter Cumulative — {_ist_h:02d}:{_ist_m:02d} IST", _greeter_cumulative)
        )

# Custom slots: 7 PM, 7:30 PM (already in loop above), 8 PM IST
REGULAR_OUTBOUND_SCHEDULE += [
    (13, 30, "generate_outbound_report.py", "Regular Outbound Cumulative — 19:00 IST", _regular_ob_cumulative),
    (14, 31, "generate_outbound_report.py", "Regular Outbound Cumulative — 20:00 IST", _regular_ob_cumulative),
]
GREETER_SCHEDULE += [
    (13, 32, "generate_greeter_report.py", "Greeter Cumulative — 19:00 IST", _greeter_cumulative),
    (14, 32, "generate_greeter_report.py", "Greeter Cumulative — 20:00 IST", _greeter_cumulative),
]

# UTC 18:00 = IST 23:30 → EOD full day reports (Regular Outbound + Greeter)
REGULAR_OUTBOUND_SCHEDULE.append(
    (18, 0, "generate_outbound_report.py",
     "Regular Outbound EOD Today — 23:30 IST", _regular_ob_eod_today)
)
GREETER_SCHEDULE.append(
    (18, 2, "generate_greeter_report.py",
     "Greeter EOD Today — 23:30 IST", _greeter_eod_today)
)

# ─── Callback Reports (Today's Queue + Overdue Alert) ────────────────────────
# Every 3 hours, 6 AM – 9 PM IST → UTC 00:30, 03:30, 06:30, 09:30, 12:30, 15:30
# (midnight and 3 AM IST slots skipped — no callback activity overnight)
CALLBACK_SCHEDULE = []
for _utc_h in range(0, 16, 3):
    _ist_h = (_utc_h + 5) % 24
    CALLBACK_SCHEDULE.append(
        (_utc_h, 30, "generate_callback_reports.py",
         f"Callback Reports — {_ist_h:02d}:00 IST", None)
    )


def ist_now():
    return datetime.now(IST)


def cleanup_old_reports(max_age_days=2):
    dirs = [
        os.path.join(BASE_DIR, 'Automation Cron Job', 'Outbound Report'),
        os.path.join(BASE_DIR, 'Automation Cron Job', 'Inbound Report'),
        os.path.join(BASE_DIR, 'Automation Cron Job', 'Greeter Report'),
        os.path.join(BASE_DIR, 'callinsight_downloads'),
        os.path.join(BASE_DIR, 'greeter_downloads'),
    ]
    cutoff = datetime.now().timestamp() - max_age_days * 86400
    deleted = 0
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for fname in os.listdir(d):
            if not fname.endswith(('.html', '.png', '.csv')):
                continue
            fpath = os.path.join(d, fname)
            if os.path.getmtime(fpath) < cutoff:
                os.remove(fpath)
                deleted += 1
    if deleted:
        print(f'[cleanup] Deleted {deleted} old report file(s) (>{max_age_days}d)', flush=True)


def run_script(script, label, args_fn=None, extra_env=None):
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
            env=extra_env,
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
    scheduler = BlockingScheduler(timezone=pytz.UTC)

    for hour, minute, script, label, args_fn in SCHEDULE:
        scheduler.add_job(
            run_script,
            trigger=CronTrigger(hour=hour, minute=minute),
            args=[script, label, args_fn],
            id=label,
        )

    for hour, minute, script, label, args_fn in CALL_SCHEDULE:
        scheduler.add_job(
            run_script,
            trigger=CronTrigger(hour=hour, minute=minute),
            args=[script, label, args_fn],
            id=label,
        )

    for hour, minute, script, label, args_fn in REGULAR_OUTBOUND_SCHEDULE:
        scheduler.add_job(
            run_script,
            trigger=CronTrigger(hour=hour, minute=minute),
            args=[script, label, args_fn],
            id=label,
        )

    for hour, minute, script, label, args_fn in GREETER_SCHEDULE:
        scheduler.add_job(
            run_script,
            trigger=CronTrigger(hour=hour, minute=minute),
            args=[script, label, args_fn],
            id=label,
        )

    for hour, minute, script, label, args_fn in CALLBACK_SCHEDULE:
        scheduler.add_job(
            run_script,
            trigger=CronTrigger(hour=hour, minute=minute),
            args=[script, label, args_fn],
            id=label,
        )

    scheduler.add_job(
        cleanup_old_reports,
        trigger=CronTrigger(hour=18, minute=30),  # midnight IST
        id='cleanup_old_reports',
    )

    print("=" * 70, flush=True)
    print("  REPORT CRON SERVER — APScheduler (UTC)", flush=True)
    print("=" * 70, flush=True)
    print(f"\n  Server time (UTC): {datetime.now(pytz.UTC).strftime('%Y-%m-%d %H:%M:%S %Z')}", flush=True)
    print(f"  Server time (IST): {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S %Z')}\n", flush=True)

    print("  SCHEDULE:", flush=True)
    for h, m, _, label, _ in SCHEDULE + CALL_SCHEDULE + REGULAR_OUTBOUND_SCHEDULE + GREETER_SCHEDULE + CALLBACK_SCHEDULE:
        ist_h = (h + 5) % 24
        ist_m = m + 30
        if ist_m >= 60:
            ist_m -= 60
            ist_h = (ist_h + 1) % 24
        print(f"    {h:02d}:{m:02d} UTC ({ist_h:02d}:{ist_m:02d} IST)  —  {label}", flush=True)

    print(f"\n{'─' * 70}", flush=True)
    print("  Scheduler started. Waiting for jobs...\n", flush=True)

    # ── DEPLOYMENT SMOKE TEST ───────────────────────────────────────────
    # Runs AFTER scheduler.start() so cron jobs are never missed.
    # Only runs when SMOKE_TEST=1 is set in the environment.
    if os.getenv('SMOKE_TEST') == '1':
        _SMOKE_GROUP = "120363426619711887@g.us"
        _smoke_env = {
            **os.environ,
            "WHATSAPP_GROUP_ONLINE_LOB":     _SMOKE_GROUP,
            "WHATSAPP_GROUP_ONLINE_LMS":    _SMOKE_GROUP,
            "WHATSAPP_GROUP_REGULAR_LMS":   _SMOKE_GROUP,
            "WHATSAPP_GROUP_DAILY_UPDATES": _SMOKE_GROUP,
            "WHATSAPP_GROUP":               _SMOKE_GROUP,
        }

        def _run_smoke_test():
            _now_ist = datetime.now(IST)
            _smoke_from = "09:30"
            _smoke_to   = (_now_ist - timedelta(minutes=15)).strftime('%H:%M')
            print("=" * 70, flush=True)
            print(f"  DEPLOY SMOKE TEST — Sending reports to admin group only ({_SMOKE_GROUP})...", flush=True)
            print(f"  CallInsight creds: email={os.getenv('CALLINSIGHT_EMAIL', 'NOT SET')!r}  password_len={len((os.getenv('CALLINSIGHT_PASSWORD') or '').strip())}", flush=True)
            print("=" * 70, flush=True)
            run_script("generate_all_lms_reports.py",    "SMOKE TEST — LMS Reports (admin group only)",          None,            extra_env=_smoke_env)
            run_script("generate_all_recon_reports.py",  "SMOKE TEST — Recon Report (yesterday full day)",       _yesterday_full, extra_env=_smoke_env)
            run_script("generate_outbound_report.py",    "SMOKE TEST — Outbound Cumulative (admin group only)",
                       lambda: ['--from-time', _smoke_from, '--to-time', _smoke_to, '--group', _SMOKE_GROUP], extra_env=_smoke_env)
            run_script("generate_greeter_report.py",     "SMOKE TEST — Greeter Cumulative (admin group only)",
                       lambda: ['--from-time', _smoke_from, '--to-time', _smoke_to, '--group', _SMOKE_GROUP], extra_env=_smoke_env)
            print("=" * 70, flush=True)
            print("  SMOKE TEST COMPLETE — Admin group notified. Scheduler is live.\n", flush=True)

        from datetime import timezone
        import threading
        threading.Thread(target=_run_smoke_test, daemon=True).start()
        print("  ℹ️  Smoke test running in background thread — scheduler is live.\n", flush=True)
    else:
        print("  ℹ️  Smoke test skipped (set SMOKE_TEST=1 to enable on deploy).\n", flush=True)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("\n  Shutting down scheduler...", flush=True)
        scheduler.shutdown(wait=False)


if __name__ == '__main__':
    main()
