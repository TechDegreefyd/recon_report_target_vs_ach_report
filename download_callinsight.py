import asyncio
import os
from playwright.async_api import async_playwright

LOGIN_URL    = "https://app.callinsight.io"
CALL_LOGS_URL = "https://app.callinsight.io/call-logs"
EMAIL    = "sid@degreefyd.com"
PASSWORD = "Nuvora#2026"
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "callinsight_downloads")


async def download_call_logs():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        # ── Login ─────────────────────────────────────────────────────────────
        print("Logging in...")
        await page.goto(LOGIN_URL)
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(2000)

        await page.fill('input[name="email"]', EMAIL)
        await page.fill('input[type="password"]', PASSWORD)
        await page.click('button[type="submit"]')

        await page.wait_for_url(lambda url: 'login' not in url, timeout=15000)
        await page.wait_for_load_state("networkidle")
        print(f"Logged in — {page.url}")

        # ── Navigate to Call Logs ─────────────────────────────────────────────
        await page.goto(CALL_LOGS_URL)
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(3000)

        # ── Download CSV ──────────────────────────────────────────────────────
        print("Downloading CSV...")
        async with page.expect_download(timeout=30000) as download_info:
            await page.click('button:has-text("Download CSV")')

        download = await download_info.value
        save_path = os.path.join(DOWNLOAD_DIR, download.suggested_filename or "call_logs.csv")
        await download.save_as(save_path)
        print(f"Downloaded: {save_path}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(download_call_logs())
