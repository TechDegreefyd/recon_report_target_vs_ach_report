#!/usr/bin/env python3
"""
Minimal script: log in to CallInsight and download the CSV.
Usage:  python download_callinsight.py
Saves the CSV to callinsight_downloads/ next to this file.
"""

import asyncio
import os
from dotenv import load_dotenv
from playwright.async_api import async_playwright

_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_DIR, '.env'))

CI_EMAIL    = (os.getenv('CALLINSIGHT_EMAIL')    or '').strip()
CI_PASSWORD = (os.getenv('CALLINSIGHT_PASSWORD') or '').strip()
SAVE_DIR    = os.path.join(_DIR, 'callinsight_downloads')


async def download():
    if not CI_EMAIL or not CI_PASSWORD:
        raise RuntimeError('Set CALLINSIGHT_EMAIL and CALLINSIGHT_PASSWORD in .env')

    os.makedirs(SAVE_DIR, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page    = await context.new_page()

        # ── Login ──────────────────────────────────────────────────────────────
        print(f'Logging in as {CI_EMAIL!r} ...')
        await page.goto('https://app.callinsight.io', timeout=90_000, wait_until='domcontentloaded')
        await page.wait_for_timeout(2000)

        await page.fill('input[name="email"]',    CI_EMAIL)
        await page.fill('input[type="password"]', CI_PASSWORD)
        await page.click('button[type="submit"]')

        try:
            await page.wait_for_url(lambda url: 'login' not in url, timeout=90_000)
        except Exception:
            snippet = (await page.inner_text('body'))[:300].replace('\n', ' ')
            print(f'Login failed — still on: {page.url}')
            print(f'Page snippet: {snippet}')
            raise

        print(f'Logged in. Current URL: {page.url}')

        # ── Navigate to call logs ──────────────────────────────────────────────
        print('Navigating to /call-logs ...')
        await page.goto('https://app.callinsight.io/call-logs', timeout=90_000, wait_until='domcontentloaded')
        await page.wait_for_timeout(4000)

        # ── Download CSV ───────────────────────────────────────────────────────
        print('Clicking Download CSV ...')
        async with page.expect_download(timeout=90_000) as dl_info:
            await page.click('button:has-text("Download CSV")')

        dl        = await dl_info.value
        save_path = os.path.join(SAVE_DIR, dl.suggested_filename or 'call_logs.csv')
        await dl.save_as(save_path)
        print(f'Saved → {save_path}')

        await browser.close()
    return save_path


if __name__ == '__main__':
    path = asyncio.run(download())
    print(f'\nDone. File: {path}')
