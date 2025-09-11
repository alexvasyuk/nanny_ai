#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
nash_scrape.py
Purpose: Reuse logged-in session from nash_login.py and open the filtered SERP.
"""

from pathlib import Path
from playwright.sync_api import sync_playwright

# Storage state created by nash_login.py
STORAGE_STATE_PATH = Path("data/session.json")

# Your exact SERP URL
SERP_URL = (
    'https://nashanyanya.ru/nyanya/moscow;query=%7B%22notSmoke%22%3Atrue%2C%22sortOrder%22%3A1'
    '%2C%22withPhoto%22%3Atrue%2C%22yearExperience%22%3A3%2C%22sortBy%22%3A%22Relevance%22'
    '%2C%22liveInOuts%22%3A%5B2%5D%2C%22employments%22%3A%5B1%2C2%5D%2C%22ageGroups%22%3A%5B3%2C4%5D'
    '%2C%22workingExperienceAges%22%3A%5B4%2C5%2C6%5D%2C%22hasAudioMessage%22%3Atrue%7D'
)


def main():
    if not STORAGE_STATE_PATH.exists():
        print(f"[ERROR] Storage state not found at {STORAGE_STATE_PATH}")
        print("Run nash_login.py first to generate it.")
        return 1

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(storage_state=str(STORAGE_STATE_PATH))
        page = context.new_page()

        print(f"[INFO] Navigating to SERP: {SERP_URL}")
        page.goto(SERP_URL, wait_until="domcontentloaded")

        # Wait a bit so you can confirm it loaded
        page.wait_for_timeout(5000)

        print("[INFO] SERP loaded. Close the browser window to finish.")
        page.wait_for_event("close")

        context.close()
        browser.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
