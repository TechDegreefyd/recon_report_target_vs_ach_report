"""
Competitor blog alert — new AND updated blog posts → WhatsApp digest.

Watches College Vidya, Jaro Education, Shiksha and Learning Routes with
Scrapling and messages when a blog is published or edited.

Sources, and why there is more than one
  * Sitemap  - the full list of posts. Complete, but it can lag a fresh post,
               and only some sitemaps carry a usable modified time.
  * Recent   - each site's own "latest posts" listing. Fast and newest-first,
               so a post shows up here before (or without) the sitemap.
  * Page     - the article itself, read only for the few posts that need a
               real publish / modified date. Never trust a page blindly: Jaro's
               JSON-LD dates are just the moment of the request, so Jaro is read
               from its page data instead (see page_info).

New     = a URL never seen before, published within NEW_MAX_AGE_DAYS. An old
          post that merely appears in a sitemap for the first time is recorded
          silently rather than announced.
Updated = the page's real modified date moved past what we last recorded, on or
          after yesterday, and after the publish date. At most one alert per post
          per day.

Update detection per site
  College Vidya, Shiksha : sitemap lastmod changed -> confirm on the page.
  Jaro, Learning Routes  : no sitemap lastmod, so the recent listing is checked
                           every run and a full crawl runs once a day (--deep).

First run for a site is a silent baseline (thousands of old posts must never
be announced). State only ever grows, so a failed or partial fetch can never
look like "posts were deleted and re-published". Sources that break are
reported by a WhatsApp health warning after HEALTH_STREAK runs in a row, once.

Usage:
    python daily_blog_alert.py                 # scrape + send
    python daily_blog_alert.py --local         # print the digest; send and save nothing
    python daily_blog_alert.py --deep          # also crawl every Jaro / Learning Routes post
    python daily_blog_alert.py --only Jaro     # restrict to one site (testing)
"""

import argparse
import base64
import gzip
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import escape, unescape
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from filelock import FileLock
from scrapling.fetchers import Fetcher

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

load_dotenv(HERE.parent / ".env")

import daily_ad_alert as ads  # noqa: E402  (WHAPI sender + recipient parsing)

STATE_FILE = Path(os.getenv("BLOG_ALERT_STATE_FILE") or (HERE / "seen_blogs.json"))

# Own recipients if set, otherwise the same people who get the competitor ad
# digest - it is the same audience watching the same competitors.
BLOG_RECIPIENTS = [
    r
    for r in (
        ads.normalize_recipient(v)
        for v in (
            os.getenv("WHATSAPP_COMPETITOR_BLOGS_TO", "").split(",")
            + os.getenv("WHATSAPP_COMPETITOR_BLOGS_TO_2", "").split(",")
        )
    )
    if r
] or ads.ALERT_RECIPIENTS

IST = timezone(timedelta(hours=5, minutes=30))
MAX_CHARS = 3500
MAX_NEW_FETCHES = 120      # page reads for new posts, per site per run
MAX_LM_CANDIDATES = 250    # page reads for sitemap-lastmod changes, per site per run
RECENT_CHECK = 15          # newest listing posts re-checked for edits every run
NEW_MAX_AGE_DAYS = 10      # older than this at first sight = old post, recorded silently
HEALTH_STREAK = 2          # consecutive bad runs before a health warning is sent
DROP_RATIO = 0.7           # sitemap shrinking below this share of the last count = broken
WORKERS = 6
FETCH_TIMEOUT = 45

log = logging.getLogger("blog_alert")


# --------------------------------------------------------------------------
# fetching (Scrapling)
# --------------------------------------------------------------------------


def fetch(url: str, tries: int = 3):
    """GET with retries. A 404/410 is final; anything else is retried, since
    these sites throw the odd 5xx or reset a connection."""
    last = None
    for attempt in range(tries):
        try:
            resp = Fetcher.get(url, stealthy_headers=True, timeout=FETCH_TIMEOUT, follow_redirects=True)
            if resp.status == 200:
                return resp
            last = RuntimeError(f"HTTP {resp.status} for {url}")
            if resp.status in (404, 410):
                break
        except Exception as exc:
            last = exc
        time.sleep(2 * (attempt + 1))
    raise last


def body_text(resp) -> str:
    raw = resp.body if isinstance(resp.body, bytes) else str(resp.body).encode("utf-8")
    # Some servers hand back a .gz untouched, others transparently unzip it.
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", "ignore")


def norm(url: str) -> str:
    """Identity of a post: host + path, no query/fragment/trailing slash, so
    the same post reached through the sitemap and a listing is one post."""
    p = urlparse(url.strip())
    return f"{p.scheme}://{p.netloc.lower()}{p.path.rstrip('/')}"


# --------------------------------------------------------------------------
# dates
# --------------------------------------------------------------------------


def parse_day(value) -> str:
    """Any date-ish string -> 'YYYY-MM-DD' in IST, or ''. Timezone-less
    timestamps are UTC (College Vidya's page data matches its UTC sitemap)."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%B %d, %Y").strftime("%Y-%m-%d")   # Jaro: "September 11, 2026"
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if len(value) <= 10:
        return dt.strftime("%Y-%m-%d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%Y-%m-%d")


def pretty_date(iso: str) -> str:
    try:
        day = datetime.strptime(iso, "%Y-%m-%d")
    except ValueError:
        return ""
    # Year only when it is not this year, so an old post never reads as recent.
    return day.strftime("%d %b" if day.year == datetime.now(IST).year else "%d %b %Y")


# --------------------------------------------------------------------------
# sitemaps
# --------------------------------------------------------------------------

LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S)
LASTMOD_RE = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>", re.S)
URL_BLOCK_RE = re.compile(r"<url>(.*?)</url>", re.S)


def sitemap_urls(sitemap: str) -> dict[str, str]:
    """url -> lastmod ('' when the sitemap has none). Parsed per <url> block:
    a lastmod belongs to its own entry, never the next one's."""
    out = {}
    for block in URL_BLOCK_RE.findall(body_text(fetch(sitemap))):
        loc = LOC_RE.search(block)
        if loc:
            lm = LASTMOD_RE.search(block)
            out[unescape(loc.group(1))] = lm.group(1) if lm else ""
    return out


def collect_sitemap(site: dict) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """({norm: (url, lastmod)}, [errors]). A child sitemap that fails is
    reported but does not discard the ones that worked - adding URLs is always
    safe, and the health check flags the gap."""
    errors, maps = [], list(site.get("sitemaps") or [])
    if site.get("index"):
        try:
            idx = body_text(fetch(site["index"]))
            maps += [unescape(u) for u in LOC_RE.findall(idx) if site["pick"].search(u)]
        except Exception as exc:
            errors.append(f"index: {exc}")
    found: dict[str, tuple[str, str]] = {}
    for sm in maps:
        try:
            for url, lm in sitemap_urls(sm).items():
                if site["include"].search(url):
                    found[norm(url)] = (url, lm)
        except Exception as exc:
            errors.append(f"{sm.rsplit('/', 1)[-1]}: {exc}")
    return found, errors


# --------------------------------------------------------------------------
# "recent posts" listings - one adapter per site, newest first
# --------------------------------------------------------------------------

NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def next_data(html: str) -> dict:
    m = NEXT_DATA_RE.search(html)
    return json.loads(m.group(1)) if m else {}


def recent_college_vidya() -> list[dict]:
    data = next_data(body_text(fetch("https://collegevidya.com/blog/")))
    page = (data.get("props") or {}).get("pageProps") or {}
    out = []
    for post in (page.get("sixRecentBlogs") or {}).get("data") or []:
        if post.get("slug"):
            out.append({
                "url": f"https://collegevidya.com/blog/{post['slug']}/",
                "title": " ".join(unescape(post.get("title") or "").split()),
                "modified": parse_day(post.get("updated_at")),
            })
    return out


def recent_jaro() -> list[dict]:
    """Jaro's listing carries each post's real publish date."""
    data = next_data(body_text(fetch("https://www.jaroeducation.com/blog/")))
    posts = (((data.get("props") or {}).get("pageProps") or {}).get("blogsData") or {}).get("data") or []
    return [
        {
            "url": f"https://www.jaroeducation.com/blog/{p['slug']}",
            "title": " ".join(unescape(p.get("title") or "").split()),
            "published": parse_day(p.get("date")),
        }
        for p in posts
        if p.get("slug")
    ]


def recent_shiksha() -> list[dict]:
    """updates.xml is Shiksha's feed of everything touched recently (news,
    exam pages and articles); keep the articles, newest first."""
    rows = [
        (lm, url)
        for url, lm in sitemap_urls("https://www.shiksha.com/updates.xml").items()
        if SHIKSHA_ARTICLE.search(url)
    ]
    return [{"url": url, "modified": parse_day(lm)} for lm, url in sorted(rows, reverse=True)[:50]]


def recent_learning_routes() -> list[dict]:
    html = body_text(fetch("https://www.learningroutes.in/blog"))
    seen, out = set(), []
    for path in re.findall(r'href="(/blog/[a-z0-9][^"#?/]*)"', html):
        if path not in seen:
            seen.add(path)
            out.append({"url": f"https://www.learningroutes.in{path}"})
    return out


# --------------------------------------------------------------------------
# sites
# --------------------------------------------------------------------------

SHIKSHA_ARTICLE = re.compile(r"/articles/.+blogId-\d+$")

# `include` keeps real posts only - category / listing pages live in the same
# sitemaps and would otherwise fire as "new blogs". `page_dates` says how to
# read real publish/modified dates off an article page.
SITES = [
    {
        "name": "College Vidya",
        "sitemaps": ["https://collegevidya.com/sitemaps/blogs.xml.gz"],
        "include": re.compile(r"^https://collegevidya\.com/blog/(?!category/|tag/|author/)[^/?#]+/?$"),
        "recent": recent_college_vidya,
        "page_dates": "jsonld",
    },
    {
        "name": "Jaro Education",
        "sitemaps": ["https://www.jaroeducation.com/blog-sitemap.xml"],
        "include": re.compile(r"^https://www\.jaroeducation\.com/blog/(?!category/|tag/|author/)[^/?#]+/?$"),
        "recent": recent_jaro,
        # Its JSON-LD datePublished/dateModified are the time of the request,
        # not the post's. The real dates are in the page data.
        "page_dates": "jaro",
    },
    {
        "name": "Shiksha",
        "index": "https://www.shiksha.com/sitemap_index.xml",
        # Articles only. The news / live-update sitemaps publish every few
        # minutes and would bury the digest.
        "pick": re.compile(r"www_(?:blog|saarticles)_SiteMap_f\d+\.xml\.gz$", re.I),
        "include": SHIKSHA_ARTICLE,
        "recent": recent_shiksha,
        "page_dates": "jsonld",
    },
    {
        "name": "Learning Routes",
        "sitemaps": ["https://www.learningroutes.in/sitemap.xml"],
        "include": re.compile(r"^https://www\.learningroutes\.in/blog/[^/?#]+/?$"),
        "recent": recent_learning_routes,
        "page_dates": "jsonld",
        # Every URL carries the time the sitemap was regenerated, so lastmod
        # says nothing about the post. Ignored, or each regeneration would
        # look like the whole blog was edited.
        "lastmod": False,
    },
]


# --------------------------------------------------------------------------
# article pages
# --------------------------------------------------------------------------

JSONLD_RE = {
    "published": re.compile(r'"datePublished"\s*:\s*"([^"]+)"'),
    "modified": re.compile(r'"dateModified"\s*:\s*"([^"]+)"'),
}


def slug_title(url: str) -> str:
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"-blogId-\d+$", "", slug)
    return slug.replace("-", " ").strip().capitalize() or url


AUTHOR_BIO = re.compile(r"\b(is|has been) an? .{0,60}(writer|marketer|author|editor|content)", re.I)


def first_paragraph(html: str, limit: int = 400) -> str:
    """First real paragraph of the article: skips short bits and author bios."""
    html = re.sub(r"(?is)<(script|style|nav|footer|header|noscript)\b.*?</\1>", "", html)
    for m in re.finditer(r"(?is)<p\b[^>]*>(.*?)</p>", html):
        text = " ".join(unescape(re.sub(r"<[^>]+>", " ", m.group(1))).split())
        if len(text) > 80 and not AUTHOR_BIO.search(text[:140]):
            return text if len(text) <= limit else text[:limit - 3].rsplit(" ", 1)[0] + "..."
    return ""


def page_info(site: dict, url: str) -> dict:
    """{title, published, modified} read from the article itself; dates are
    'YYYY-MM-DD' or '' when the page does not declare them. Failures return
    empty fields rather than raising - one dead page must not sink a run."""
    info = {"title": "", "summary": "", "published": "", "modified": ""}
    try:
        resp = fetch(url, tries=2)
        html = body_text(resp)
        for sel in ('meta[property="og:title"]::attr(content)', "title::text"):
            value = resp.css(sel).get()
            if value and value.strip():
                info["title"] = " ".join(unescape(value).split())
                break
        desc = (resp.css('meta[name="description"]::attr(content)').get()
                or resp.css('meta[property="og:description"]::attr(content)').get() or "")
        desc = " ".join(unescape(desc).split())
        body = html
        if site["name"] == "College Vidya":       # article body lives in the page data, not the HTML shell
            detail = ((next_data(html).get("props") or {}).get("pageProps") or {}).get("blogDetail") or {}
            body = detail.get("content") or html
        info["summary"] = first_paragraph(body) or (desc if len(desc) <= 300 else desc[:297].rsplit(" ", 1)[0] + "...")
        if site["page_dates"] == "jaro":
            page = ((next_data(html).get("props") or {}).get("pageProps") or {}).get("data") or {}
            page = page.get("data") or {}
            info["published"] = parse_day(page.get("publish_date"))
            info["modified"] = parse_day(page.get("modified_date"))
        else:
            for key in ("published", "modified"):
                meta = resp.css(f'meta[property="article:{key}_time"]::attr(content)').get()
                if not meta:
                    m = JSONLD_RE[key].search(html)
                    meta = m.group(1) if m else ""
                info[key] = parse_day(meta)
    except Exception as exc:
        log.warning("page read failed for %s: %s", url, exc)
    return info


def read_pages(site: dict, urls: list[str]) -> dict[str, dict]:
    if not urls:
        return {}
    with ThreadPoolExecutor(WORKERS) as pool:
        return dict(zip(urls, pool.map(lambda u: page_info(site, u), urls)))


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------


def empty_state() -> dict:
    return {"version": 2, "sites": {}, "pending": [], "health": {"streak": {}, "alerted": [], "pending": []}}


def load_state() -> dict:
    """{"sites": {name: {"seen": {norm: lastmod}, "mod": {norm: 'YYYY-MM-DD'}, "count": n}},
        "pending": [{kind, site, url, title, published, modified, to}],
        "health": {"streak": {issue: n}, "alerted": [issue], "pending": [{text, to}]}}"""
    state = empty_state()
    if not STATE_FILE.exists():
        return state
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # Unreadable state would re-baseline every site silently, so posts
        # published while it was broken are lost - say so loudly.
        log.error("state file unreadable (%s) - every site will re-baseline", exc)
        return state
    if not isinstance(data, dict):
        return state
    if isinstance(data.get("sites"), dict):
        state["sites"] = data["sites"]
    elif isinstance(data.get("seen"), dict):   # v1: {"seen": {site: [urls]}}
        state["sites"] = {
            name: {"seen": {norm(u): "" for u in urls}, "mod": {}, "count": len(urls)}
            for name, urls in data["seen"].items()
            if isinstance(urls, list)
        }
    if isinstance(data.get("pending"), list):
        state["pending"] = [p for p in data["pending"] if isinstance(p, dict) and p.get("kind")]
    if isinstance(data.get("health"), dict):
        state["health"].update({k: v for k, v in data["health"].items() if k in state["health"]})
    return state


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    tmp.replace(STATE_FILE)   # atomic: a crash mid-write can never leave a half file


# --------------------------------------------------------------------------
# per-site run
# --------------------------------------------------------------------------


def is_update(sd: dict, key: str, published: str, modified: str, sitemap_says_changed: bool, floor: str) -> bool:
    """Decide whether a page's modified date is an alert-worthy edit, and record
    it either way. Recording is what makes this fire at most once per post per
    day, and what turns the very first sighting into a baseline."""
    prev = sd["mod"].get(key)
    if modified:
        sd["mod"][key] = max(modified, prev or "")
    if not modified or modified < floor:
        return False
    if published and modified <= published:     # still its publish day: not an edit
        return False
    if prev is None:
        return sitemap_says_changed             # no history: only trust a sitemap-signalled change
    return modified > prev


def _neg(s: str) -> tuple:
    """Sort key that puts larger strings first inside an ascending sort."""
    return tuple(-ord(c) for c in s)


def run_site(site: dict, sd: dict | None, deep: bool, floor: str) -> tuple[list[dict], dict | None, list[str]]:
    """Returns (items, new site state, health issue keys seen this run)."""
    name, issues = site["name"], []

    sm, errors = collect_sitemap(site)
    if errors:
        log.warning("[%s] sitemap problems: %s", name, "; ".join(errors))
        issues.append(f"sitemap-partial:{name}")
    try:
        recent = site["recent"]()
    except Exception as exc:
        log.warning("[%s] recent listing failed: %s", name, exc)
        recent = []
    if not recent:
        issues.append(f"recent:{name}")
    if not sm:
        issues.append(f"sitemap-empty:{name}")

    universe = {k: url for k, (url, _) in sm.items()}
    for r in recent:
        universe.setdefault(norm(r["url"]), r["url"])
    if not universe:
        return [], sd, issues

    prev_count = (sd or {}).get("count") or 0
    dropped = bool(sm and prev_count and len(sm) < prev_count * DROP_RATIO)
    if dropped:
        log.error("[%s] sitemap shrank %d -> %d", name, prev_count, len(sm))
        issues.append(f"sitemap-drop:{name}")

    lastmod = {k: lm for k, (_, lm) in sm.items()} if site.get("lastmod", True) else {}
    recent_by_key = {norm(r["url"]): r for r in recent}
    recent_keys = list(recent_by_key)

    def carry(sd_: dict) -> dict:
        for k in universe:
            sd_["seen"].setdefault(k, "")
            if lastmod.get(k):
                sd_["seen"][k] = lastmod[k]
        if sm and not dropped:
            sd_["count"] = len(sm)
        return sd_

    if sd is None:
        log.info("[%s] first run - baselined %d existing blogs, nothing sent", name, len(universe))
        sd = carry({"seen": {}, "mod": {}, "count": 0})
        for k, r in recent_by_key.items():
            if r.get("modified"):
                sd["mod"][k] = r["modified"]
        return [], sd, issues

    seen = sd["seen"]
    new_keys = [k for k in universe if k not in seen]
    # Spend the capped page reads on the freshest: newest listing posts first,
    # then by sitemap lastmod.
    order = {k: i for i, k in enumerate(recent_keys)}
    new_keys.sort(key=lambda k: (order.get(k, len(order)), _neg(lastmod.get(k, ""))))
    new_set = set(new_keys)

    changed = sorted(
        (k for k in universe if k in seen and lastmod.get(k) and seen[k] and lastmod[k] != seen[k]),
        key=lambda k: lastmod[k], reverse=True,
    )
    if len(changed) > MAX_LM_CANDIDATES:
        log.warning("[%s] %d sitemap changes - checking the newest %d (bulk edit?)", name, len(changed), MAX_LM_CANDIDATES)
    changed = changed[:MAX_LM_CANDIDATES]
    changed_set = set(changed)

    recheck = [k for k in recent_keys[:RECENT_CHECK] if k in seen and k not in changed_set]
    if deep and not lastmod:
        recheck = [k for k in universe if k in seen and k not in changed_set]
        log.info("[%s] deep crawl of %d posts", name, len(recheck))

    read_keys = new_keys[:MAX_NEW_FETCHES] + changed + recheck
    pages = read_pages(site, [universe[k] for k in read_keys])
    info = {k: pages[universe[k]] for k in read_keys}
    log.info("[%s] %d in sitemap, %d new, %d sitemap-changed, %d re-checked",
             name, len(sm), len(new_keys), len(changed), len(recheck))

    cutoff = (datetime.now(IST) - timedelta(days=NEW_MAX_AGE_DAYS)).strftime("%Y-%m-%d")
    items = []
    for k in new_keys:
        rec, page = recent_by_key.get(k, {}), info.get(k, {})
        published = rec.get("published") or page.get("published") or ""
        modified = page.get("modified") or rec.get("modified") or published
        if published and published < cutoff:
            log.info("[%s] old post newly listed (published %s), recorded silently: %s", name, published, universe[k])
        else:
            items.append({
                "kind": "new", "site": name, "url": universe[k],
                "title": rec.get("title") or page.get("title") or slug_title(universe[k]),
                "published": published, "modified": modified,
                "summary": page.get("summary", ""),
            })
        if modified:
            sd["mod"][k] = modified

    for k in changed + recheck:
        page, rec = info.get(k, {}), recent_by_key.get(k, {})
        published = page.get("published") or rec.get("published") or ""
        modified = page.get("modified") or rec.get("modified") or ""
        if is_update(sd, k, published, modified, k in changed_set, floor):
            items.append({
                "kind": "updated", "site": name, "url": universe[k],
                "title": page.get("title") or rec.get("title") or slug_title(universe[k]),
                "published": published, "modified": modified,
                "summary": page.get("summary", ""),
            })

    return items, carry(sd), issues


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------

HEALTH_TEXT = {
    "sitemap-empty": "sitemap returned nothing",
    "sitemap-partial": "part of the sitemap could not be read",
    "sitemap-drop": "sitemap shrank sharply",
    "recent": "the latest-posts listing returned nothing",
}


def update_health(state: dict, issues: set[str], sites_run: list[str]) -> None:
    """A source has to stay broken for HEALTH_STREAK runs before anyone is
    told (one flaky request is not an incident), and is reported once until it
    recovers."""
    h = state["health"]
    streak, alerted = h["streak"], set(h["alerted"])
    live = {i for i in streak if i.split(":", 1)[1] in sites_run} | issues
    for issue in live:
        kind, site = issue.split(":", 1)
        if issue in issues:
            streak[issue] = streak.get(issue, 0) + 1
            if streak[issue] >= HEALTH_STREAK and issue not in alerted:
                h["pending"].append({
                    "text": f"⚠️ *Blog watcher* — {site}: {HEALTH_TEXT.get(kind, kind)}. "
                            f"New-blog alerts for this site may be late or missing.",
                    "to": list(BLOG_RECIPIENTS),
                })
                alerted.add(issue)
        else:
            streak.pop(issue, None)
            if issue in alerted:
                alerted.discard(issue)
                h["pending"].append({"text": f"✅ *Blog watcher* — {site}: recovered.", "to": list(BLOG_RECIPIENTS)})
    h["alerted"] = sorted(alerted)


# --------------------------------------------------------------------------
# message
# --------------------------------------------------------------------------


def item_lines(it: dict, index: int) -> list[str]:
    tail = []
    if it["kind"] == "updated":
        tail.append(f"Updated {pretty_date(it['modified'])}" if it.get("modified") else "Updated")
        if it.get("published"):
            tail.append(f"Published {pretty_date(it['published'])}")
    elif it.get("published"):
        tail.append(f"Published {pretty_date(it['published'])}")
    url = f"🔗 {it['url']}" + (" · " + " · ".join(tail) if tail else "")
    lines = [f"{ads.num_label(index)} {it['title']}", url]
    if it.get("summary"):
        lines.append(f"_{it['summary']}_")
    return lines + [""]


def build_messages(items: list[dict]) -> list[str]:
    n_new = sum(i["kind"] == "new" for i in items)
    n_upd = len(items) - n_new
    sites = len({i["site"] for i in items})
    counts = " · ".join(
        p for p in (f"{n_new} new" if n_new else "", f"{n_upd} updated" if n_upd else "") if p
    )
    header = (
        f"📝 *Competitor Blogs* — {datetime.now(IST).strftime('%a, %d %b')}\n"
        f"_{counts} · {sites} {'competitor' if sites == 1 else 'competitors'}_"
    )
    divider = "━━━━━━━━━━━━━━━━━━━━"

    by_site: dict[str, list[dict]] = {}
    for it in items:
        by_site.setdefault(it["site"], []).append(it)

    # No cap on posts: every post is listed. Long digests are split into
    # several WhatsApp messages between posts, repeating the site heading.
    msgs, current = [], ""

    def add(text):
        nonlocal current
        if current and len(current) + len(text) + 1 > MAX_CHARS:
            msgs.append(current)
            current = ""
        current = f"{current}\n{text}" if current else text

    for name, rows in sorted(by_site.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        new = sorted((r for r in rows if display_kind(r) == "new"), key=lambda r: r.get("published") or "", reverse=True)
        upd = sorted((r for r in rows if display_kind(r) == "updated"), key=lambda r: r.get("modified") or r.get("published") or "", reverse=True)
        parts = [f"{len(new)} new" if new else "", f"{len(upd)} updated" if upd else ""]
        add(f"{divider}\n*{name}* — " + " · ".join(p for p in parts if p) + "\n")
        for label, group in (("🆕 *New*", new), ("✏️ *Updated*", upd)):
            if not group:
                continue
            add(label + "\n")
            for i, it in enumerate(group, 1):
                add("\n".join(item_lines(it, i)))
    if current:
        msgs.append(current.rstrip())

    if len(msgs) == 1:
        return [f"{header}\n{msgs[0]}"]
    return [
        f"{header}\n{part}" if i == 1 else f"_(continued {i}/{len(msgs)})_\n{part}"
        for i, part in enumerate(msgs, 1)
    ]


HTML_CSS = """
:root{--bg:#eef1f8;--card:#fff;--ink:#161c2c;--mute:#5b6472;--line:#e3e7f0;--new:#0a7d4f;--newbg:#e8f7ef;--upd:#b45309;--updbg:#fdf1dc;--link:#1d4ed8;--accent1:#4f46e5;--accent2:#0ea5e9;--chip:#eef0fb}
@media(prefers-color-scheme:dark){:root{--bg:#0b0f19;--card:#151b2b;--ink:#e9edf7;--mute:#93a0b8;--line:#262f45;--new:#4ade80;--newbg:#123324;--upd:#fbbf24;--updbg:#3a2a0f;--link:#8ab4ff;--accent1:#818cf8;--accent2:#38bdf8;--chip:#1c2338}
  img,.card{filter:none}}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:860px;margin:0 auto;padding:0 16px 40px}
.hero{background:linear-gradient(135deg,var(--accent1),var(--accent2));color:#fff;margin:0 -16px 18px;padding:28px 16px 22px;border-radius:0 0 18px 18px}
.hero h1{font-size:23px;margin:0 0 4px;letter-spacing:-.01em}
.hero .date{opacity:.92;font-size:13.5px;margin-bottom:14px}
.stats{display:flex;gap:8px;flex-wrap:wrap}
.stat{background:rgba(255,255,255,.16);backdrop-filter:blur(4px);border-radius:10px;padding:8px 12px;min-width:76px}
.stat b{display:block;font-size:19px;line-height:1.1}
.stat span{font-size:11px;opacity:.9;text-transform:uppercase;letter-spacing:.04em}
.toc{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 22px}
.toc a{background:var(--card);border:1px solid var(--line);border-radius:999px;padding:6px 13px;font-size:12.5px;font-weight:600;color:var(--ink);text-decoration:none;display:flex;gap:6px;align-items:center}
.toc a:hover{border-color:var(--accent1)}
.toc .n{background:var(--chip);border-radius:999px;padding:1px 7px;font-size:11px;color:var(--mute)}
.site{margin:0 0 30px}
.site h2{font-size:18px;margin:0 0 10px;padding-bottom:8px;border-bottom:2px solid var(--line);display:flex;align-items:baseline;gap:8px;scroll-margin-top:14px}
.site h2 small{font-weight:400;color:var(--mute);font-size:12.5px}
h3.kind{font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;margin:16px 0 9px;display:flex;align-items:center;gap:7px}
h3.kind.new{color:var(--new)}h3.kind.upd{color:var(--upd)}
h3.kind::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor;display:inline-block}
.card{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--line);border-radius:10px;padding:13px 15px;margin:0 0 9px;transition:border-color .15s}
.card.new{border-left-color:var(--new)}
.card.upd{border-left-color:var(--upd)}
.card a.title{color:var(--ink);font-weight:600;font-size:14.5px;text-decoration:none;display:block;margin-bottom:4px}
.card a.title:hover{color:var(--link)}
.badge{display:inline-block;font-size:10.5px;font-weight:700;letter-spacing:.03em;text-transform:uppercase;border-radius:5px;padding:2px 7px;margin-right:7px;vertical-align:1px}
.badge.new{color:var(--new);background:var(--newbg)}
.badge.upd{color:var(--upd);background:var(--updbg)}
.meta{color:var(--mute);font-size:12px;margin:0 0 7px}
.meta a{color:var(--link);text-decoration:none;font-size:11.5px}
.txt{margin:0;color:var(--ink);font-size:13.5px;line-height:1.55;opacity:.9}
footer{color:var(--mute);font-size:11.5px;text-align:center;margin-top:28px;padding-top:16px;border-top:1px solid var(--line)}
"""


def display_kind(it: dict) -> str:
    """'new' only for a post actually published today; anything else - including
    a 'new' post first discovered today for an older article - displays as
    'updated', since it is not news from today."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if it["kind"] == "new" and it.get("published") and it["published"] != today:
        return "updated"
    return it["kind"]


def build_html(items: list[dict]) -> str:
    n_new = sum(display_kind(i) == "new" for i in items)
    n_upd = len(items) - n_new
    by_site: dict[str, list[dict]] = {}
    for it in items:
        by_site.setdefault(it["site"], []).append(it)
    day = datetime.now(IST).strftime("%a, %d %b %Y")
    anchor = lambda name: re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    ordered = sorted(by_site.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    out = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        "<meta name=viewport content='width=device-width,initial-scale=1'>",
        f"<title>Competitor Blogs {day}</title><style>{HTML_CSS}</style></head><body><div class=wrap>",
        "<div class=hero><h1>\U0001f4dd Competitor Blogs</h1>",
        f"<div class=date>{escape(day)}</div>",
        "<div class=stats>",
        f"<div class=stat><b>{n_new}</b><span>New</span></div>",
        f"<div class=stat><b>{n_upd}</b><span>Updated</span></div>",
        f"<div class=stat><b>{len(by_site)}</b><span>Sites</span></div>",
        "</div></div>",
        "<div class=toc>",
    ]
    for name, rows in ordered:
        out.append(f"<a href='#{anchor(name)}'>{escape(name)}<span class=n>{len(rows)}</span></a>")
    out.append("</div>")

    for name, rows in ordered:
        new = sorted((r for r in rows if display_kind(r) == "new"), key=lambda r: r.get("published") or "", reverse=True)
        upd = sorted((r for r in rows if display_kind(r) == "updated"), key=lambda r: r.get("modified") or r.get("published") or "", reverse=True)
        out.append(f"<div class=site><h2 id='{anchor(name)}'>{escape(name)} <small>{len(new)} new &middot; {len(upd)} updated</small></h2>")
        for cls, label, group in (("new", "New", new), ("upd", "Updated", upd)):
            if not group:
                continue
            out.append(f"<h3 class='kind {cls}'>{label} &middot; {len(group)}</h3>")
            for it in group:
                meta = []
                if cls == "upd":
                    stamp = it.get("modified") if it["kind"] == "updated" else it.get("published")
                    label_word = "Updated" if it["kind"] == "updated" else "Spotted"
                    if stamp:
                        meta.append(f"{label_word} " + pretty_date(stamp))
                if it.get("published"):
                    meta.append("Published " + pretty_date(it["published"]))
                domain = urlparse(it["url"]).netloc.replace("www.", "")
                out.append(
                    f"<div class='card {cls}'>"
                    f"<span class='badge {cls}'>{'New' if cls == 'new' else 'Updated'}</span>"
                    f"<a class=title href='{escape(it['url'], quote=True)}' target=_blank rel=noopener>{escape(it['title'])}</a>"
                    f"<div class=meta>{escape(' &middot; '.join(meta)).replace('&amp;middot;','&middot;')} &middot; <a href='{escape(it['url'], quote=True)}' target=_blank rel=noopener>{escape(domain)} \u2197</a></div>"
                    + (f"<p class=txt>{escape(it['summary'])}</p>" if it.get("summary") else "")
                    + "</div>"
                )
        out.append("</div>")

    out.append(f"<footer>Generated {datetime.now(IST).strftime('%d %b %Y, %H:%M IST')} &middot; scraped from each site&rsquo;s sitemap and blog listing</footer>")
    out.append("</div></body></html>")
    return "".join(out)


def build_summary(items: list[dict]) -> str:
    n_new = sum(display_kind(i) == "new" for i in items)
    n_upd = len(items) - n_new
    lines = [
        f"📝 *Competitor Blogs* — {datetime.now(IST).strftime('%a, %d %b')}",
        f"_{n_new} new · {n_upd} updated_",
        "",
    ]
    counts: dict[str, list[int]] = {}
    for it in items:
        counts.setdefault(it["site"], [0, 0])[display_kind(it) == "updated"] += 1
    for name, (a, b) in sorted(counts.items(), key=lambda kv: -sum(kv[1])):
        lines.append(f"• *{name}* — " + " · ".join(p for p in (f"{a} new" if a else "", f"{b} updated" if b else "") if p))
    lines += ["", "📎 Full list with links + first paragraph is in the attached HTML file."]
    return chr(10).join(lines)


def send_html(html: str, filename: str, caption: str, gid: str) -> bool:
    payload = {
        "to": gid,
        "media": f"data:text/html;name={filename};base64," + base64.b64encode(html.encode("utf-8")).decode(),
        "caption": caption,
    }
    headers = {"accept": "application/json", "authorization": f"Bearer {ads.WHAPI_TOKEN}", "content-type": "application/json"}
    try:
        resp = requests.post("https://gate.whapi.cloud/messages/document", headers=headers, json=payload, timeout=120)
    except requests.RequestException as exc:
        log.error("WHAPI document failed -> %s: %s", gid, exc)
        return False
    if 200 <= resp.status_code < 300:
        log.info("html sent -> %s", gid)
        return True
    log.error("WHAPI HTTP %s -> %s: %s", resp.status_code, gid, resp.text[:160])
    return False


def deliver_digest(gid: str, items: list[dict]) -> bool:
    """One short summary text plus the full digest as an HTML file."""
    name = f"competitor_blogs_{datetime.now(IST).strftime('%Y-%m-%d_%H%M')}.html"
    if not send_html(build_html(items), name, "Competitor blogs — full list", gid):
        return False
    return ads.send_text(build_summary(items), gid)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def pending_key(it: dict) -> tuple:
    return (it["kind"], it["url"], it.get("modified") if it["kind"] == "updated" else "")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true", help="Print the digest instead of sending; state is not saved")
    parser.add_argument("--deep", action="store_true", help="Also crawl every post on sites whose sitemap has no modified dates")
    parser.add_argument("--only", help="Only run sites whose name contains this text (testing)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("scrapling").setLevel(logging.WARNING)

    if not args.local and not BLOG_RECIPIENTS:
        raise SystemExit("WHATSAPP_COMPETITOR_BLOGS_TO / WHATSAPP_COMPETITOR_ADS_TO not set in .env (or use --local)")
    if not args.local and not ads.WHAPI_TOKEN:
        raise SystemExit("WHAPI_TOKEN_PAID not set in .env (or use --local)")

    # The daily deep crawl outlasts the next regular run; without this the two
    # would load the same state and the later save would erase the other's.
    with FileLock(str(STATE_FILE) + ".lock", timeout=3600):
        run(args)


def run(args):
    state = load_state()
    floor = (datetime.now(IST) - timedelta(days=2)).strftime("%Y-%m-%d")   # edits from 2 days ago on (Shiksha sitemap lags ~1 day)
    issues, ran = set(), []

    for site in SITES:
        if args.only and args.only.lower() not in site["name"].lower():
            continue
        name = site["name"]
        ran.append(name)
        try:
            items, sd, site_issues = run_site(site, state["sites"].get(name), args.deep, floor)
        except Exception as exc:
            log.error("[%s] FAILED: %s", name, exc, exc_info=True)
            issues.add(f"sitemap-empty:{name}")
            continue
        issues.update(site_issues)
        if sd is not None:
            state["sites"][name] = sd
        queued = {pending_key(p) for p in state["pending"]}
        for it in items:
            if pending_key(it) not in queued:
                state["pending"].append({**it, "to": list(BLOG_RECIPIENTS)})

    update_health(state, issues, ran)

    if args.local:
        if issues:
            print("Health issues this run:", ", ".join(sorted(issues)))
        if not state["pending"]:
            print("No new or updated blogs.")
        else:
            for msg in build_messages(state["pending"]):
                print("\n" + "=" * 60 + "\n" + msg)
        log.info("--local: nothing sent, state file not updated")
        return

    any_failed = False
    for gid in BLOG_RECIPIENTS:
        mine = [it for it in state["pending"] if gid in it["to"]]
        if mine:
            if deliver_digest(gid, mine):
                for it in mine:
                    it["to"].remove(gid)
            else:
                any_failed = True
                log.error("send failed → %s - its blogs stay pending and retry next run", gid)
        for note in state["health"]["pending"]:
            if gid in note["to"] and ads.send_text(note["text"], gid):
                note["to"].remove(gid)

    state["pending"] = [it for it in state["pending"] if it["to"]]
    state["health"]["pending"] = [n for n in state["health"]["pending"] if n["to"]]
    save_state(state)
    if any_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
