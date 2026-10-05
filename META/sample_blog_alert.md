# Sample: Competitor Blog Alert (WhatsApp message)

Sample from a local test on Fri, 25 Sep 2026. Nothing was sent to WhatsApp.
Posts and dates are real, read from the sites. To trigger the alert I told the
watcher it had not yet seen a few of them, and marked a few older posts as
"edited earlier".

## What the WhatsApp message looks like

📝 **Competitor Blogs** — Fri, 25 Sep
*3 new · 2 updated · 3 competitors*

━━━━━━━━━━━━━━━━━━━━
**Jaro Education** — 2 new · 1 updated

🆕 **New**

1️⃣ Difference Between SQL and MySQL: Key Differences & Examples
🔗 https://www.jaroeducation.com/blog/difference-between-sql-and-mysql · Published 24 Sep

2️⃣ Difference Between Correlation and Regression: Key Differences & Examples
🔗 https://www.jaroeducation.com/blog/difference-between-correlation-and-regression · Published 23 Sep

✏️ **Updated**

1️⃣ IBPS PO 2026 Exam Guide: Dates, Salary, Eligibility
🔗 https://www.jaroeducation.com/blog/ibps-po-exam · Updated 24 Sep · Published 10 Apr

━━━━━━━━━━━━━━━━━━━━
**College Vidya** — 1 new

🆕 **New**

1️⃣ Online DBA vs PhD: Completion Rate & Success Comparison
🔗 https://collegevidya.com/blog/online-dba-vs-phd-completion-rate/ · Published 21 Sep

━━━━━━━━━━━━━━━━━━━━
**Shiksha** — 1 updated

✏️ **Updated**

1️⃣ Top 10 NLUs in India
🔗 https://www.shiksha.com/law/articles/top-10-nlus-in-india-blogId-188598 · Updated 25 Sep · Published 24 Jan 2025

A quiet run (nothing new, nothing edited) sends **no message at all**.

## How new and updated posts are detected

| Site | New posts | Edited posts |
|---|---|---|
| College Vidya | sitemap + its "recent blogs" list | sitemap modified-time changed, confirmed on the page |
| Jaro Education | sitemap + its blog listing (real dates) | its recent posts every run, plus a full daily crawl |
| Shiksha | article sitemaps + its updates feed | sitemap modified-time changed, confirmed on the page |
| Learning Routes | sitemap + its blog listing | its recent posts every run, plus a full daily crawl |

Learning Routes' sitemap dates are ignored (every URL carries the regeneration
time). Jaro's page JSON-LD dates are ignored (they are just the request time);
Jaro's real dates come from its page data.

## Rules that keep it from being noisy

| Rule | Why |
|---|---|
| First run per site is a silent baseline | thousands of old posts must not be announced |
| A "new" URL published more than 10 days ago is recorded silently | an old post newly appearing in a sitemap is not a new blog |
| Updated = real modified date moved forward, since yesterday, and after the publish date | a post edited on its publish day is just "new" |
| At most one update alert per post per day | repeated edits don't repeat the alert |
| State only grows | a failed fetch can never look like "posts deleted, then re-published" |

## Schedule and health

- Regular check: every 2 hours, 9:00 AM – 9:00 PM IST.
- Deep crawl of Jaro + Learning Routes (about 4,500 pages, ~15 min): once a day at 9:15 PM IST.
- If a source stays broken for 2 runs in a row (empty sitemap, sitemap shrinks by 30%+, listing
  returns nothing), a single ⚠️ warning goes to WhatsApp; a ✅ notice follows when it recovers.
- A failed WhatsApp send retries on the next run, for that recipient only.
