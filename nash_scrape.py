#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path
from datetime import datetime
from playwright.sync_api import sync_playwright
from scorer import score_with_chatgpt

from extractors import (
    open_first_profile_from_serp, 
    extract_name_from_profile, 
    extract_age_from_profile,
    extract_experience_from_profile,
    extract_about_from_profile, 
    extract_education_from_profile,
    extract_recommendations_from_profile,
)
from io_csv import append_row

# Reuse session saved by nash_login.py
STORAGE_STATE_PATH = Path("data/session.json")

# Your encoded SERP URL (with hasAudioMessage=true)
SERP_URL = (
    'https://nashanyanya.ru/nyanya/moscow;query=%7B%22notSmoke%22%3Atrue%2C%22sortOrder%22%3A1'
    '%2C%22withPhoto%22%3Atrue%2C%22yearExperience%22%3A3%2C%22sortBy%22%3A%22Relevance%22'
    '%2C%22liveInOuts%22%3A%5B2%5D%2C%22employments%22%3A%5B1%2C2%5D%2C%22ageGroups%22%3A%5B3%2C4%5D'
    '%2C%22workingExperienceAges%22%3A%5B4%2C5%2C6%5D%2C%22hasAudioMessage%22%3Atrue%7D'
)

# Where to write results
OUTPUT_CSV = Path("data/nannies.csv")

JD_PATH = Path("data/jd.txt")

def main():
    if not STORAGE_STATE_PATH.exists():
        print(f"[ERROR] Storage state not found at {STORAGE_STATE_PATH}. Run nash_login.py first.")
        return 1

    if not JD_PATH.exists():
        print(f"[ERROR] JD file not found at {JD_PATH}")
        return 1

    jd_text = JD_PATH.read_text(encoding="utf-8").strip()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(storage_state=str(STORAGE_STATE_PATH))
        page = context.new_page()

        print(f"[INFO] Opening SERP: {SERP_URL}")
        page.goto(SERP_URL, wait_until="domcontentloaded")

        # Open first profile from SERP
        profile_page = open_first_profile_from_serp(page)
        print("[INFO] On profile:", profile_page.url)

        # Extract fields (for now: name)
        name = extract_name_from_profile(profile_page)
        age = extract_age_from_profile(profile_page)
        experience_years = extract_experience_from_profile(profile_page) 
        about_content = extract_about_from_profile(profile_page)
        education = extract_education_from_profile(profile_page)
        rec_count, rec_texts = extract_recommendations_from_profile(profile_page) 

        # Use scorer.py
        fit_score = score_with_chatgpt(
            jd_text=jd_text,
            about_text=about_content,
        )

        # Build a row and write to CSV
        row = {
            "scraped_at": datetime.now().isoformat(timespec="seconds"),
            "profile_url": profile_page.url,
            "name": name,
            "age": age,
            "experience_years": experience_years, 
            "education": education,
            "recommendations_count": rec_count,
            "recommendations_texts": " REC END. | REC START: ".join(rec_texts) if rec_texts else "", 
            "about_content": about_content,
            "fit_score": fit_score, 
        }
        append_row(row, OUTPUT_CSV)
        print(f"[INFO] Wrote row to {OUTPUT_CSV}")

        # (Optional) Keep window open briefly for visual check
        profile_page.wait_for_timeout(2000)

        context.close()
        browser.close()

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
