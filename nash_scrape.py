#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
nash_scrape.py
Purpose: Reuse logged-in session from nash_login.py and open the filtered SERP.
"""

from pathlib import Path
from playwright.sync_api import sync_playwright

import re
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

# Storage state created by nash_login.py
STORAGE_STATE_PATH = Path("data/session.json")

# Your exact SERP URL
SERP_URL = (
    'https://nashanyanya.ru/nyanya/moscow;query=%7B%22notSmoke%22%3Atrue%2C%22sortOrder%22%3A1'
    '%2C%22withPhoto%22%3Atrue%2C%22yearExperience%22%3A3%2C%22sortBy%22%3A%22Relevance%22'
    '%2C%22liveInOuts%22%3A%5B2%5D%2C%22employments%22%3A%5B1%2C2%5D%2C%22ageGroups%22%3A%5B3%2C4%5D'
    '%2C%22workingExperienceAges%22%3A%5B4%2C5%2C6%5D%2C%22hasAudioMessage%22%3Atrue%7D'
)

PROFILE_URL_RE = re.compile(r"/nyanya/moscow/\d+$")

def open_first_profile_from_serp(page, timeout=15000):
    """
    On the SERP, open the first nanny's profile.
    Strategy:
      A) Click the first <a href="/nyanya/moscow/..."> inside the first card
      B) Fallback: click the first 'Подробнее' button in that card
    Then wait for SPA routing to /nyanya/moscow/<id>.
    """
    # Wait for the first card to be rendered
    first_card = page.locator("nn-nanny-resume-card").first
    first_card.wait_for(state="visible", timeout=timeout)

    # A) Try the direct profile link inside the card
    link = first_card.locator("a[href^='/nyanya/moscow/']").first
    if link.count() > 0:
        link.scroll_into_view_if_needed()
        link.click()
    else:
        # B) Fallback: the "Подробнее" chevron button
        more_btn = first_card.locator("button.button-chevron, .card-resume__more .button-chevron").first
        if more_btn.count() == 0:
            # last resort: text lookup (non-semantic)
            more_btn = first_card.get_by_text("Подробнее", exact=False).first
        more_btn.scroll_into_view_if_needed()
        more_btn.click()

    # Wait for SPA URL change, then fallback to a profile-only element
    try:
        page.wait_for_url(PROFILE_URL_RE, timeout=timeout)
        return page
    except PlaywrightTimeoutError:
        try:
            page.locator("text=НАПИСАТЬ, a[href^='tel:']").first.wait_for(state="visible", timeout=timeout)
            return page
        except PlaywrightTimeoutError:
            print("[WARN] Clicked into profile but URL and profile-only UI did not appear.")
            print("[DEBUG] Current URL:", page.url)
            return page


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

        # Open the first nanny profile
        profile_page = open_first_profile_from_serp(page)
        print("[INFO] Opened profile:", profile_page.url)

        # Keep the window open to inspect
        profile_page.wait_for_timeout(5000)
        print("[INFO] Close the window to exit.")
        profile_page.wait_for_event("close")

        context.close()
        browser.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
