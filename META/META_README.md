# Meta Ad Library Competitor Scraper

## What this does
For a given advertiser (competitor), pulls every ad their Facebook Page
has run in India in the last N days from Meta's public Ad Library, with
clean structured data per ad: real ad copy, landing URL, CTA, image/video
creative URLs, start date, active platforms.

## How it works
A real Playwright-controlled Chromium browser is pointed at:
```
https://www.facebook.com/ads/library/?...&search_type=page&view_all_page_id=<page_id>
```
(the same page-scoped Ad Library view you'd get clicking into an
advertiser manually - no login required, it's public data).

Instead of scraping the rendered page's HTML/text (which is broken up
across hundreds of nested `<span>`s and comes out mangled), the scraper
reads the same structured JSON the page itself is built from:

- **Initial batch**: for advertisers with few ads, all of them are
  embedded directly in the page's server-rendered HTML on first load.
- **Rest of the ads**: as you scroll, Facebook fires
  `AdLibrarySearchPaginationQuery` GraphQL requests to fetch more - the
  scraper listens for these responses directly.

Both sources return identical shaped data (`ad_library_main.search_
results_connection.edges[].node.collated_results[]`), so one function
(`flatten_result`) turns either into a clean row: `library_id`,
`page_name`, `start_date`, `ad_text`, `landing_url`, `cta_text`,
`title`, `display_format`, `image_urls`, `video_urls`.

## The DCO/catalog wrinkle
Some ads are Dynamic Creative Optimization (catalog) ads. Their
top-level text is just an unfilled template like `{{product.brand}}` -
but the real, human-readable copy is usually still present one level
down, in the ad's `cards[]` array. The scraper checks for this and
falls back to the first card's text/image/link whenever the top-level
text is just a placeholder, so real content doesn't get thrown away.
Ads where even the cards have no real text are excluded by default
(`--include-dco` to keep them anyway).

## Usage
Single competitor:
```
python scraper.py --page-id <fb_page_id> --name "Competitor Name" --days 15 --out results.csv
```

Multiple competitors in one run, one combined CSV:
```
python scraper.py --pages-file competitors.json --days 15 --out all_competitors.csv
```
`competitors.json` is a simple list: `[{"page_id": "...", "name": "..."}, ...]`.

## Known limitations
- Meta only discloses spend/demographic breakdown for political & issue
  ads, not for regular commercial ads (`ad_type=all`) - that data isn't
  in the API response at all for these, not a scraper gap.
- Some video ads fail to play in the Ad Library UI itself ("Sorry, we're
  having trouble with playing this video") for everyone, logged in or
  not - a Facebook-side issue, no video URL is retrievable for those.
