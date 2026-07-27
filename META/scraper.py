"""
Meta Ad Library scraper - competitor page mode (Playwright + GraphQL replay).

Fetches all ads run by a specific advertiser's Facebook Page (via
view_all_page_id) in the Meta Ad Library, keeping only ads whose
start_date falls within the last N days (default 15).

Instead of scraping the rendered DOM (whose text is broken up by
countless nested spans and mangles badly under innerText/regex),
this drives a real browser to the Ad Library page and reads the same
structured `ad_library_main.search_results_connection` data the page
itself uses - clean ad body text, the real landing URL, and direct
HD video / image URLs, with no DOM parsing involved.

Pagination is cursor-driven, not scroll-driven. The browser is used
only to (a) render the first page, whose ads are embedded in the
server-rendered HTML, and (b) fire one `AdLibrarySearchPaginationQuery`
so we can capture its authenticated request. From there we replay that
request ourselves with the connection's own `end_cursor`, and stop when
`page_info.has_next_page` is false. That is Meta's own termination
signal - no scroll heuristics, no stagnation counters, no sleeps.

Collection and filtering are kept strictly separate: every ad is
fetched first, then the date window and the DCO rule are applied once
at the end. (The Ad Library sorts by relevancy, not by date, so
anything that stops paging on "first old ad" silently loses recent
ads ranked further down.)

DCO / catalog ads - whose copy is an unfilled `{{product.brand}}`-style
template rather than real creative - are dropped from the output. Pass
--include-dco to keep them.

Note: Meta only discloses spend range / demographic breakdown for
political & issue ads, not for ad_type=all (regular commercial ads).
That data simply isn't present in this API response for these ads.

Usage:
    python scraper.py --page-id 101876854628849 --name "College Vidya" --days 15
    python scraper.py --pages-file competitors.json --days 15 --out results.csv
"""

import argparse
import csv
import json
import logging
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

BASE_URL = (
    "https://www.facebook.com/ads/library/"
    "?active_status=active&ad_type=all&country=IN&is_targeted_country=false"
    "&media_type=all&search_type=page"
    "&sort_data[mode]=relevancy_monthly_grouped&sort_data[direction]=desc"
    "&view_all_page_id={page_id}"
)

GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
PAGINATION_QUERY = "AdLibrarySearchPaginationQuery"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

FIELDS = [
    "competitor",
    "library_id",
    "ad_library_url",
    "page_name",
    "is_active",
    "start_date",
    "collation_id",
    "collation_count",
    "variants_captured",
    "publisher_platform",
    "cta_text",
    "title",
    "ad_text",
    "landing_url",
    "display_format",
    "image_urls",
    "video_urls",
]

log = logging.getLogger("adlib")


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

# Any unfilled Liquid-style token anywhere in the copy marks the ad as DCO.
# Matching only a body that is *entirely* a placeholder misses the common
# mixed case ("Get {{product.name}} today"), which is just as unreadable.
PLACEHOLDER_RE = re.compile(r"\{\{.*?\}\}", re.S)


def has_placeholder(text: str) -> bool:
    return bool(text) and bool(PLACEHOLDER_RE.search(text))


TEXT_FIELDS = ("ad_text", "title", "cta_text")


def strip_placeholders(row: dict) -> dict:
    """Blank any text field that still contains an unfilled template token,
    so no '{{product.name}}' ever reaches the output. Done per field rather
    than per ad: catalog ads very often pair a templated title with real,
    human-written body copy, and that copy is the thing worth keeping."""
    for field in TEXT_FIELDS:
        if has_placeholder(row.get(field) or ""):
            row[field] = ""
    return row


def is_dco(row: dict) -> bool:
    """Judge on the body copy, which is what makes an ad readable. A row is
    dropped only when it has no usable body text and no usable title left
    after placeholders are stripped."""
    return not (row.get("ad_text") or "").strip() and not (row.get("title") or "").strip()


def get_friendly_name(request) -> str:
    post = request.post_data or ""
    for part in post.split("&"):
        if part.startswith("fb_api_req_friendly_name="):
            return part.split("=", 1)[1]
    return ""


def flatten_result(cr: dict, competitor: str) -> dict:
    """DCO/catalog ads often carry no usable top-level snapshot.body.text
    (just an unfilled '{{product.brand}}'-style template) even though the
    actual creative copy - identical across the ad's card variants in
    practice - lives in snapshot.cards[]. Fall back to the first card
    whenever the top-level text is just a template placeholder."""
    snap = cr.get("snapshot") or {}
    body_text = ((snap.get("body") or {}).get("text") or "").strip()
    cards = snap.get("cards") or []

    title = snap.get("title") or ""
    cta_text = snap.get("cta_text") or ""
    landing_url = snap.get("link_url") or ""
    images = [
        img.get("original_image_url") or img.get("resized_image_url") or ""
        for img in (snap.get("images") or [])
    ]
    videos = [
        v.get("video_hd_url") or v.get("video_sd_url") or ""
        for v in (snap.get("videos") or [])
    ]

    ad_text = body_text
    if (not ad_text or has_placeholder(ad_text)) and cards:
        first = cards[0]
        card_body = (first.get("body") or "").strip()
        if card_body:
            ad_text = card_body
        title = title or first.get("title") or ""
        cta_text = cta_text or first.get("cta_text") or ""
        landing_url = landing_url or first.get("link_url") or ""
    for c in cards:
        img = c.get("original_image_url") or c.get("resized_image_url") or ""
        if img:
            images.append(img)
        vid = c.get("video_hd_url") or c.get("video_sd_url") or ""
        if vid:
            videos.append(vid)

    start_ts = cr.get("start_date")
    start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc) if start_ts else None
    lib_id = str(cr.get("ad_archive_id") or "")

    return {
        "competitor": competitor,
        "library_id": lib_id,
        "ad_library_url": f"https://www.facebook.com/ads/library/?id={lib_id}" if lib_id else "",
        "page_name": cr.get("page_name") or snap.get("page_name") or "",
        "is_active": cr.get("is_active"),
        "start_date": start_dt.strftime("%Y-%m-%d") if start_dt else "",
        "_start_dt": start_dt,
        "collation_id": str(cr.get("collation_id") or ""),
        "collation_count": cr.get("collation_count") or 1,
        "publisher_platform": ", ".join(cr.get("publisher_platform") or []),
        "cta_text": cta_text,
        "title": title,
        "ad_text": ad_text,
        "landing_url": landing_url,
        "display_format": snap.get("display_format") or "",
        "image_urls": [u for u in dict.fromkeys(images) if u],
        "video_urls": [u for u in dict.fromkeys(videos) if u],
    }


def iter_results(edges):
    """Most edges wrap their ads in `collated_results`; some carry the ad
    directly on the node. Handle both so grouped and ungrouped ads are
    counted alike."""
    for edge in edges or []:
        node = edge.get("node") or {}
        collated = node.get("collated_results")
        for cr in collated if collated else [node]:
            if isinstance(cr, dict) and cr.get("ad_archive_id"):
                yield cr


def find_ad_library_main(node):
    """Recursively search a JSON blob for the ad_library_main object that
    holds search_results_connection. Used against the SSR-embedded JSON
    since its shape/nesting around this object isn't fixed."""
    if isinstance(node, dict):
        main = node.get("ad_library_main")
        if isinstance(main, dict) and "search_results_connection" in main:
            return main
        for v in node.values():
            found = find_ad_library_main(v)
            if found is not None:
                return found
    elif isinstance(node, list):
        for v in node:
            found = find_ad_library_main(v)
            if found is not None:
                return found
    return None


def extract_ssr_connection(html: str) -> dict:
    """Ads for low-volume pages are embedded directly in the initial
    server-rendered HTML rather than fetched via GraphQL XHR - Facebook
    only fires the pagination query when there's genuinely more to load.
    Returns a connection-shaped dict (edges + page_info)."""
    edges, page_info = [], {}
    for script in re.findall(r'<script type="application/json"[^>]*>(.*?)</script>', html, re.S):
        if "ad_library_main" not in script:
            continue
        try:
            data = json.loads(script)
        except json.JSONDecodeError:
            continue
        main = find_ad_library_main(data)
        if not main:
            continue
        conn = main.get("search_results_connection") or {}
        edges.extend(conn.get("edges") or [])
        page_info = page_info or (conn.get("page_info") or {})
    return {"edges": edges, "page_info": page_info}


# --------------------------------------------------------------------------
# pagination
# --------------------------------------------------------------------------


def parse_json_stream(text: str) -> dict:
    """Facebook sometimes streams GraphQL responses as newline-delimited
    JSON objects; the first one holds the payload we want."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return {}


def connection_of(payload: dict) -> dict:
    main = (payload.get("data") or {}).get("ad_library_main") or {}
    return main.get("search_results_connection") or {}


def replay_headers(headers: dict) -> dict:
    """Facebook rejects the GraphQL call (error 1357054) unless the CSRF-ish
    fetch metadata is present, and Playwright's request context doesn't add
    it for us. Carry over everything the browser sent except the hop-by-hop
    headers, then fill in what a same-origin XHR would have."""
    out = {k: v for k, v in headers.items() if k.lower() not in ("host", "content-length")}
    out.setdefault("origin", "https://www.facebook.com")
    out.setdefault("accept", "*/*")
    out.setdefault("sec-fetch-site", "same-origin")
    out.setdefault("sec-fetch-mode", "cors")
    out.setdefault("sec-fetch-dest", "empty")
    return out


def request_variables(body: str) -> dict:
    parts = urllib.parse.parse_qs(body, keep_blank_values=True)
    return json.loads((parts.get("variables") or ["{}"])[0])


def rebuild_body(body: str, cursor: str, batch: int = 30) -> str:
    """Take the captured pagination request body and swap in a new cursor,
    leaving every other variable (page id, filters, tokens) untouched. The
    page asks for 10 ads at a time; we ask for more to cut round trips."""
    parts = urllib.parse.parse_qs(body, keep_blank_values=True)
    variables = json.loads((parts.get("variables") or ["{}"])[0])
    variables["cursor"] = cursor
    variables["first"] = batch
    parts["variables"] = [json.dumps(variables, separators=(",", ":"))]
    return urllib.parse.urlencode({k: v[0] for k, v in parts.items()})


def scrape_page(page, page_id: str, competitor: str, max_pages: int = 200) -> list[dict]:
    """Collect every ad for a page. No filtering happens here - callers
    decide what to keep."""
    captured: dict[str, str] = {}
    edges: list[dict] = []

    def on_request(req):
        if "/api/graphql/" in req.url and req.method == "POST":
            if get_friendly_name(req) == PAGINATION_QUERY and "body" not in captured:
                captured["body"] = req.post_data or ""
                captured["headers"] = replay_headers(req.headers)

    def on_response(resp):
        req = resp.request
        if "/api/graphql/" not in resp.url or req.method != "POST":
            return
        if get_friendly_name(req) != PAGINATION_QUERY:
            return
        try:
            conn = connection_of(parse_json_stream(resp.text()))
        except Exception:
            return
        edges.extend(conn.get("edges") or [])

    page.on("request", on_request)
    page.on("response", on_response)
    try:
        page.goto(BASE_URL.format(page_id=page_id), wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)

        ssr = extract_ssr_connection(page.content())
        edges.extend(ssr["edges"])
        log.info("[%s] server-rendered page: %d ads", competitor, len(list(iter_results(edges))))

        # Scroll only far enough to make Facebook fire the paginated query
        # once, so we can capture an authenticated request to replay. The
        # page lazy-renders, so this takes a few nudges. Whatever results
        # arrive meanwhile are kept - they're free - but the cursor chain is
        # seeded from the captured request itself, not from those responses:
        # they land concurrently and the last one to arrive is not
        # necessarily the furthest along.
        for _ in range(12):
            if captured:
                break
            page.mouse.wheel(0, 6000)
            page.wait_for_timeout(1500)

        page.wait_for_timeout(2000)  # let in-flight responses land
        page.remove_listener("response", on_response)

        cursor = None
        if captured:
            cursor = request_variables(captured["body"]).get("cursor")
        elif (ssr.get("page_info") or {}).get("has_next_page"):
            cursor = ssr["page_info"].get("end_cursor")
        pages_fetched = 0
        while cursor and captured and pages_fetched < max_pages:
            try:
                resp = page.request.post(
                    GRAPHQL_URL,
                    headers=captured["headers"],
                    data=rebuild_body(captured["body"], cursor),
                    timeout=45000,
                )
            except PlaywrightError as exc:
                log.warning("[%s] pagination request failed: %s", competitor, exc)
                break
            if not resp.ok:
                log.warning("[%s] pagination HTTP %s", competitor, resp.status)
                break

            conn = connection_of(parse_json_stream(resp.text()))
            new_edges = conn.get("edges") or []
            if not new_edges:
                break
            edges.extend(new_edges)
            pages_fetched += 1

            info = conn.get("page_info") or {}
            next_cursor = info.get("end_cursor") if info.get("has_next_page") else None
            if next_cursor == cursor:  # defensive: never loop on a stuck cursor
                break
            cursor = next_cursor
            log.info("[%s] page %d -> %d ads so far", competitor, pages_fetched, len(edges))

        if cursor and not captured:
            log.warning(
                "[%s] more ads exist but no pagination request was captured; "
                "results may be incomplete",
                competitor,
            )
    finally:
        page.remove_listener("request", on_request)
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    rows, seen = [], set()
    for cr in iter_results(edges):
        row = flatten_result(cr, competitor)
        if row["library_id"] in seen:
            continue
        seen.add(row["library_id"])
        rows.append(row)
    log.info("[%s] collected %d unique ads", competitor, len(rows))
    return rows


def annotate_variants(rows: list[dict]) -> None:
    """Meta groups near-identical creatives into a collation and reports the
    group size as collation_count. Every variant it returns is already its
    own row here, but the rows carry no marker tying them together - so a
    concept running 15 variants reads as 15 unrelated ads.

    collation_id makes those rows groupable (sort by it to see one concept's
    whole variant set). variants_captured is how many of that group survived
    into this file, which is often fewer than collation_count: Meta returns
    only a subset of each group's variants, and there's no supported way to
    expand one. Treat collation_count as the real spend signal - a concept
    with 15 variants is being pushed hard whether or not we hold all 15."""
    counts: dict[str, int] = {}
    for row in rows:
        cid = row.get("collation_id")
        if cid:
            counts[cid] = counts.get(cid, 0) + 1
    for row in rows:
        cid = row.get("collation_id")
        row["variants_captured"] = counts.get(cid, 1) if cid else 1


def select(rows: list[dict], cutoff: datetime, include_dco: bool, quiet: bool = False) -> list[dict]:
    """Apply the date window and the DCO rule once, after collection."""
    kept, dropped_old, dropped_dco = [], 0, 0
    for row in rows:
        row = dict(row)
        start_dt = row.pop("_start_dt", None)
        if start_dt and start_dt < cutoff:
            dropped_old += 1
            continue
        if not include_dco:
            row = strip_placeholders(row)
            if is_dco(row):
                dropped_dco += 1
                continue
        kept.append(row)
    annotate_variants(kept)
    if not quiet:
        log.info(
            "kept %d ads (dropped %d outside window, %d with no usable copy)",
            len(kept),
            dropped_old,
            dropped_dco,
        )
    return kept


# --------------------------------------------------------------------------
# io
# --------------------------------------------------------------------------


def load_competitors(args) -> list[dict]:
    if args.pages_file:
        return json.loads(Path(args.pages_file).read_text(encoding="utf-8"))
    if args.page_id:
        return [{"page_id": args.page_id, "name": args.name or args.page_id}]
    raise SystemExit("Provide --page-id/--name or --pages-file")


def save(results: list[dict], out_path: str, fmt: str):
    out = Path(out_path)
    if fmt == "json":
        out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        return
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in results:
            row = dict(row)
            row["image_urls"] = "; ".join(row.get("image_urls") or [])
            row["video_urls"] = "; ".join(row.get("video_urls") or [])
            writer.writerow(row)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--page-id", help="Single Facebook Page ID (view_all_page_id)")
    parser.add_argument("--name", help="Label for the single --page-id run")
    parser.add_argument("--pages-file", help="JSON file: [{\"page_id\": \"...\", \"name\": \"...\"}, ...]")
    parser.add_argument("--days", type=int, default=15)
    parser.add_argument("--out", default="results.csv")
    parser.add_argument("--format", choices=["csv", "json"], default="csv")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--max-pages", type=int, default=200, help="Safety cap on pagination requests per competitor")
    parser.add_argument("--raw-out", help="Also write every ad fetched, unfiltered, to this JSON file")
    parser.add_argument(
        "--include-dco",
        action="store_true",
        help="Keep dynamic/catalog (DCO) ads whose copy is an unfilled "
             "{{product.brand}}-style template. Excluded by default since "
             "they carry no readable creative copy.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    competitors = load_competitors(args)
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    all_raw = []
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
                    rows = scrape_page(page, comp["page_id"], comp["name"], args.max_pages)
                except Exception as exc:
                    log.error("[%s] FAILED: %s", comp["name"], exc)
                    rows = []
                finally:
                    page.close()
                all_raw.extend(rows)
                log.info("[%s] %d ads in window", comp["name"], len(select(rows, cutoff, args.include_dco, quiet=True)))
                # Checkpoint after every competitor so a later failure
                # never costs the work already done.
                save(select(all_raw, cutoff, args.include_dco, quiet=True), args.out, args.format)
        finally:
            browser.close()

    results = select(all_raw, cutoff, args.include_dco)
    save(results, args.out, args.format)
    if args.raw_out:
        raw = [{k: v for k, v in r.items() if k != "_start_dt"} for r in all_raw]
        Path(args.raw_out).write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Saved %d ads (last %d days) to %s", len(results), args.days, args.out)


if __name__ == "__main__":
    main()
