"""
Daily competitor ad alert — Meta Ad Library → WhatsApp digest.

Runs the Ad Library scraper across every competitor in competitors.json,
keeps only ads launched in the last DAYS days (default 1), and sends a
single text digest to a WhatsApp group via WHAPI.

Why a seen-set on top of the date window: the window is a moving 24-hour
cutoff, not a calendar day, so an ad launched shortly before yesterday's
run still falls inside today's window and would be reported twice. The
seen-set exists purely to suppress that overlap - it is not a backlog.
Every ad the scraper returns is recorded as seen, including ones already
outside the window, so nothing old can ever surface as "new" later. Seen
state is tracked per recipient, so a delivery failure to one WhatsApp
recipient only retries for that recipient.

Usage:
    python daily_ad_alert.py                 # scrape + send
    python daily_ad_alert.py --local         # print the digest, send nothing
    python daily_ad_alert.py --days 2        # widen the launch window
"""

import argparse
import json
import logging
import os
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from scraper import USER_AGENT, scrape_page, select  # noqa: E402

load_dotenv(HERE.parent / ".env")

COMPETITORS_FILE = HERE / "competitors.json"

# Overridable so the container can keep state on a mounted volume - a rebuild
# that wiped this file would re-send the previous day's ads.
STATE_FILE = Path(os.getenv("AD_ALERT_STATE_FILE") or (HERE / "seen_ads.json"))

WHAPI_TOKEN = os.getenv("WHAPI_TOKEN_PAID")


def normalize_recipient(value: str) -> str:
    """WHAPI's `to` accepts a group id (`...@g.us`) or a plain phone number in
    international format with no punctuation. Group ids are passed through;
    anything else is reduced to digits so a human-friendly '+91 89556 99571'
    in .env works as-is."""
    value = value.strip()
    if "@" in value:
        return value
    return "".join(ch for ch in value if ch.isdigit())


ALERT_RECIPIENTS = [
    r
    for r in (
        normalize_recipient(v)
        for v in (
            os.getenv("WHATSAPP_COMPETITOR_ADS_TO", "").split(",")
            + os.getenv("WHATSAPP_COMPETITOR_ADS_TO_2", "").split(",")
        )
    )
    if r
]

IST = timezone(timedelta(hours=5, minutes=30))

# WhatsApp hard-caps a text message around 4096 chars; split well short of
# it so a multi-byte emoji near the boundary can never push a part over.
MAX_CHARS = 3500
SNIPPET_CHARS = 150
HOT_VARIANTS = 5        # variant count at which a concept is flagged as pushed hard
STATE_RETENTION_DAYS = 30

log = logging.getLogger("ad_alert")


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------


def load_state() -> dict:
    """Map of library_id -> {"date": ISO date first seen, "pending": [gid, ...]}.

    `pending` is the set of recipients that have *not* been told about this ad
    yet. Delivery is tracked per recipient because WHAPI can fail for one
    recipient while succeeding for another - collapsing that into one flag
    would resend the whole digest to everyone who already received it.

    Missing file is a normal first run, not an error: the 1-day window keeps
    that run small. Legacy entries (a bare ISO date string, written before
    per-recipient tracking) mean "delivered to everyone".
    """
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("state file unreadable (%s) - treating every ad as new", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    state = {}
    for key, value in data.items():
        if isinstance(value, str):
            state[key] = {"date": value, "pending": []}
        elif isinstance(value, dict) and isinstance(value.get("date"), str):
            pending = value.get("pending")
            state[key] = {
                "date": value["date"],
                "pending": [g for g in pending if isinstance(g, str)] if isinstance(pending, list) else [],
            }
    return state


def pending_for(state: dict, library_id: str) -> list[str]:
    """Recipients still owed this ad - everyone, if it has never been seen."""
    entry = state.get(library_id)
    if entry is None:
        return list(ALERT_RECIPIENTS)
    return [g for g in entry["pending"] if g in ALERT_RECIPIENTS]


def mark_delivered(state: dict, library_id: str, gid: str, today_iso: str) -> None:
    entry = state.setdefault(
        library_id, {"date": today_iso, "pending": list(ALERT_RECIPIENTS)}
    )
    entry["pending"] = [g for g in entry["pending"] if g != gid]


def save_state(state: dict) -> None:
    """Prune ids well past any plausible window so the file stays small -
    an id older than the retention period can never re-enter the digest."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=STATE_RETENTION_DAYS)).date().isoformat()
    pruned = {k: v for k, v in state.items() if v["date"] >= cutoff}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(pruned, indent=0, sort_keys=True), encoding="utf-8")


# --------------------------------------------------------------------------
# message
# --------------------------------------------------------------------------


def snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    """Collapse newlines so one ad stays one visual block in the chat."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


# Meta returns these SHOUTED ("FACEBOOK", "MULTI_IMAGES"). Plain .title()
# would mangle the acronyms into "Dco"/"Dpa", so those are spelled out.
_FORMAT_NAMES = {"DCO": "Catalog", "DPA": "Catalog", "MULTI_IMAGES": "Carousel"}


def pretty_format(value: str) -> str:
    key = (value or "").strip().upper()
    if not key:
        return ""
    return _FORMAT_NAMES.get(key, key.replace("_", " ").title())


_PLATFORM_ABBR = {
    "FACEBOOK": "FB",
    "INSTAGRAM": "IG",
    "MESSENGER": "Messenger",
    "THREADS": "Threads",
    "AUDIENCE_NETWORK": "Audience Network",
}


def pretty_platforms(value: str) -> str:
    out = []
    for part in (value or "").split(","):
        key = part.strip().upper()
        if key:
            out.append(_PLATFORM_ABBR.get(key, key.replace("_", " ").title()))
    return ", ".join(out)


# Emoji digits read far better than "1." in a chat. Only 1-10 exist as single
# glyphs, so anything beyond that falls back to a plain number.
_NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def num_label(index: int) -> str:
    return _NUM_EMOJI[index - 1] if index <= len(_NUM_EMOJI) else f"{index}."


_FORMAT_ICON = {
    "VIDEO": "🎥",
    "IMAGE": "🖼️",
    "DCO": "🖼️",
    "DPA": "🖼️",
    "MULTI_IMAGES": "🖼️",
}


def pretty_date(iso: str) -> str:
    """'2026-07-26' -> '26 Jul'. Falls back to the raw value if Meta ever
    hands back something unparseable."""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d %b")
    except (ValueError, TypeError):
        return iso or ""


# Meta withholds the real landing page for some catalog/DCO ads and returns a
# bare Meta-owned stub ("http://fb.me/") instead. It conveys nothing, so the
# Landing line is dropped rather than printed as a dead link.
_STUB_HOSTS = {"fb.me", "facebook.com", "l.facebook.com", "fb.com"}


def is_stub_url(url: str) -> bool:
    """Only a *bare* Meta host counts as a stub. A Meta URL carrying a path or
    query may be a genuine destination (a Page, a lead form), so it is kept."""
    parsed = urllib.parse.urlparse((url or "").strip())
    host = parsed.netloc.lower().removeprefix("www.")
    if not host:
        return False
    return host in _STUB_HOSTS and parsed.path in ("", "/") and not parsed.query


def format_head(row: dict, index: int) -> str:
    """`1️⃣ 🎥 Video · 🔥 10 variants`. The variant count is shown only when
    there is more than one - it is the spend signal, and printing '1 variant'
    on every ad buries it in noise."""
    key = (row.get("display_format") or "").strip().upper()
    icon = _FORMAT_ICON.get(key, "🖼️")
    head = f"{num_label(index)} {icon} {pretty_format(key)}".rstrip()

    variants = row.get("collation_count") or 1
    if variants > 1:
        flame = "🔥 " if variants >= HOT_VARIANTS else ""
        head += f" · {flame}{variants} variants"
    return head


def format_ad(row: dict, index: int) -> list[str]:
    lines = [format_head(row, index)]

    body = snippet(row.get("ad_text") or row.get("title") or "")
    if body:
        lines.append(f'"{body}"')

    cta = (row.get("cta_text") or "").strip()
    landing = (row.get("landing_url") or "").strip()
    if landing and is_stub_url(landing):
        landing = ""
    if cta or landing:
        # CTA and landing share a line when both exist, since neither is long.
        parts = [f"→ CTA: {cta}"] if cta else []
        if landing:
            parts.append(f"🔗 {landing}")
        lines.append(" | ".join(parts) if cta else parts[0])

    platforms = pretty_platforms(row.get("publisher_platform"))
    started = pretty_date((row.get("start_date") or "").strip())
    meta = " · ".join(p for p in (f"📱 {platforms}" if platforms else "",
                                  f"Started {started}" if started else "") if p)
    if meta:
        lines.append(meta)

    if row.get("ad_library_url"):
        lines.append(f"🔍 {row['ad_library_url']}")

    return lines


def creative_key(row: dict) -> str:
    """Identity of the creative itself, ignoring which ad slot it runs in.
    Advertisers frequently push the same copy under several ad IDs; repeating
    the full block for each makes the digest look longer than the activity
    actually is."""
    return " ".join((row.get("ad_text") or row.get("title") or "").split()).lower()


def build_blocks(new_rows: list[dict], quiet_names: list[str]) -> list[str]:
    """One block per competitor, loudest competitor first and loudest ad
    first inside each - so the biggest spend signal is visible without
    reading the whole digest."""
    by_comp: dict[str, list[dict]] = {}
    for row in new_rows:
        by_comp.setdefault(row.get("competitor") or "Unknown", []).append(row)

    ordered = sorted(by_comp.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    blocks = []
    for name, rows in ordered:
        rows.sort(key=lambda r: -(r.get("collation_count") or 1))
        lines = [f"*{name}* ({len(rows)} new)", ""]
        first_seen: dict[str, int] = {}
        for i, row in enumerate(rows, 1):
            key = creative_key(row)
            original = first_seen.get(key) if key else None
            if original:
                # Same copy already shown above - collapse to a pointer so the
                # distinct ad ID is still reported without repeating the body.
                lines.append(
                    f"{format_head(row, i)} · _same creative as {num_label(original)}, "
                    f"different ad ID_"
                )
                if row.get("ad_library_url"):
                    lines.append(f"🔍 {row['ad_library_url']}")
            else:
                if key:
                    first_seen[key] = i
                lines.extend(format_ad(row, i))
            lines.append("")
        blocks.append("\n".join(lines).rstrip())

    if quiet_names:
        blocks.append("✅ *No new ads:* " + ", ".join(quiet_names))
    return blocks


def pretty_day(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%a, %d %b")


def build_messages(new_rows: list[dict], quiet_names: list[str], total_comps: int, target_days: list[str]) -> list[str]:
    label = pretty_day(target_days[0]) if len(target_days) == 1 else (
        f"{pretty_day(target_days[0])} – {pretty_day(target_days[-1])}"
    )

    if not new_rows:
        return [
            f"😴 *New Competitor Ads* — {label}\n"
            f"None of the {total_comps} competitors launched anything new."
        ]

    active = len({r.get("competitor") for r in new_rows})
    plural = "ad" if len(new_rows) == 1 else "ads"
    comp_word = "competitor" if active == 1 else "competitors"
    header = (
        f"🆕 *New Competitor Ads* — {label}\n"
        f"_Meta Ad Library · India · {len(new_rows)} new {plural} · {active} {comp_word}_"
    )
    divider = "━━━━━━━━━━━━━━━━━━━━"

    # Pack competitor blocks into as few messages as fit, never splitting a
    # block, so a single ad is never cut across two messages.
    parts, current = [], ""
    for block in build_blocks(new_rows, quiet_names):
        chunk = f"{divider}\n{block}"
        if current and len(current) + len(chunk) + 2 > MAX_CHARS:
            parts.append(current)
            current = chunk
        else:
            current = f"{current}\n\n{chunk}" if current else chunk
    if current:
        parts.append(current)

    # Divider sits directly under the header - no blank line between them.
    if len(parts) == 1:
        return [f"{header}\n{parts[0]}"]
    return [
        f"{header.rstrip('_')} ({i}/{len(parts)})_\n{part}" if i == 1
        else f"_(continued {i}/{len(parts)})_\n{part}"
        for i, part in enumerate(parts, 1)
    ]


# --------------------------------------------------------------------------
# send
# --------------------------------------------------------------------------


def send_text(body: str, gid: str) -> bool:
    headers = {
        "accept": "application/json",
        "authorization": f"Bearer {WHAPI_TOKEN}",
        "content-type": "application/json",
    }
    try:
        resp = requests.post(
            "https://gate.whapi.cloud/messages/text",
            headers=headers,
            json={"to": gid, "body": body},
            # Generous: WHAPI has been observed taking >20s to ack a long
            # text. Timing out early risks the message landing anyway while
            # we record it as failed, which would duplicate it next run.
            timeout=90,
        )
    except requests.RequestException as exc:
        log.error("WHAPI request failed → %s: %s", gid, exc)
        return False
    if 200 <= resp.status_code < 300:
        log.info("sent → %s", gid)
        return True
    log.error("WHAPI HTTP %s → %s: %s", resp.status_code, gid, resp.text[:160])
    return False


def send_all(messages: list[str], gid: str) -> bool:
    """Send one recipient's whole digest. Stops at the first failure: the
    remaining parts belong to the same digest, so sending them out of order
    around a hole is worse than retrying the tail next run."""
    for msg in messages:
        if not send_text(msg, gid):
            return False
    return True


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1,
                        help="How many calendar days back to report, ending at --date (default 1)")
    parser.add_argument("--date", help="Last launch date to report, YYYY-MM-DD (default: yesterday IST)")
    parser.add_argument("--local", action="store_true", help="Print the digest instead of sending")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--competitors", default=str(COMPETITORS_FILE))
    parser.add_argument("--include-dco", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    if not args.local and not ALERT_RECIPIENTS:
        raise SystemExit("WHATSAPP_COMPETITOR_ADS_TO not set in .env (or use --local)")
    if not args.local and not WHAPI_TOKEN:
        raise SystemExit("WHAPI_TOKEN_PAID not set in .env (or use --local)")

    competitors = json.loads(Path(args.competitors).read_text(encoding="utf-8"))

    # Calendar days, not a rolling `now - 24h` cutoff. A rolling cutoff run at
    # 10 AM would silently drop everything launched before 10 AM the previous
    # day - i.e. most of the day we're actually reporting on.
    last_day = (
        datetime.strptime(args.date, "%Y-%m-%d").date()
        if args.date
        else (datetime.now(IST) - timedelta(days=1)).date()
    )
    target_days = sorted(
        (last_day - timedelta(days=i)).isoformat() for i in range(args.days)
    )
    log.info("reporting ads launched on: %s", ", ".join(target_days))

    state = load_state()
    today_iso = datetime.now(timezone.utc).date().isoformat()

    all_raw: list[dict] = []
    failed: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context = browser.new_context(
            viewport={"width": 1400, "height": 1200},
            locale="en-US",
            user_agent=USER_AGENT,
        )
        try:
            for comp in competitors:
                log.info("=== %s (%s) ===", comp["name"], comp["page_id"])
                page = context.new_page()
                try:
                    all_raw.extend(scrape_page(page, comp["page_id"], comp["name"], args.max_pages))
                except Exception as exc:
                    log.error("[%s] FAILED: %s", comp["name"], exc)
                    failed.append(comp["name"])
                finally:
                    page.close()
        finally:
            browser.close()

    # Filter to the target launch dates first, then hand that set to select()
    # purely for its placeholder-stripping and variant annotation - hence the
    # unreachable cutoff, which disables its own rolling date window.
    on_target = [r for r in all_raw if r.get("start_date") in target_days]
    in_window = select(on_target, datetime(1970, 1, 1, tzinfo=timezone.utc), args.include_dco)
    undelivered = [r for r in in_window if pending_for(state, r["library_id"])]
    log.info(
        "%d ads launched on target date(s), %d after filtering, %d not yet reported to everyone",
        len(on_target), len(in_window), len(undelivered),
    )

    def digest(rows: list[dict]) -> list[str]:
        # A competitor is only "quiet" if it was actually scraped successfully -
        # a failed page must never be reported as having launched nothing.
        active = {r.get("competitor") for r in rows}
        quiet = [c["name"] for c in competitors if c["name"] not in active and c["name"] not in failed]
        messages = build_messages(rows, quiet, len(competitors) - len(failed), target_days)
        if failed:
            messages[-1] += "\n\n⚠️ _Could not check: " + ", ".join(failed) + "_"
        return messages

    if args.local:
        for msg in digest(undelivered):
            print("\n" + "=" * 60)
            print(msg)
        print("\n" + "=" * 60)
        log.info("--local: nothing sent, state file not updated")
        return

    # Per recipient, because a failure to one must not cost the others their
    # delivered state - otherwise the next run resends this digest to people
    # who already got it.
    any_failed = False
    for gid in ALERT_RECIPIENTS:
        rows = [r for r in in_window if gid in pending_for(state, r["library_id"])]
        if send_all(digest(rows), gid):
            # Record every ad seen this run, not just the reported ones, so ads
            # that were already outside the window can never appear as new
            # later.
            for row in all_raw:
                mark_delivered(state, row["library_id"], gid, today_iso)
        else:
            any_failed = True
            log.error("send failed → %s - its ads stay pending and retry next run", gid)

    save_state(state)
    if any_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
