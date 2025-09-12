#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from urllib.parse import urlparse
from typing import Any, Iterable, Optional 
from pathlib import Path
from datetime import datetime
from playwright.sync_api import sync_playwright
from scorer import score_with_chatgpt
from io_csv import append_row
import re

from extractors import (
    get_serp_cards,
    open_profile_from_card,
    # existing field extractors:
    extract_name_from_profile, 
    extract_age_from_profile,
    extract_experience_from_profile,
    extract_about_from_profile, 
    extract_education_from_profile,
    extract_recommendations_from_profile,
    extract_has_audio_from_profile,
    extract_has_fairy_tale_audio,
)

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

def profile_id_from_url(u: str) -> str:
    try:
        last = urlparse(u).path.rstrip("/").split("/")[-1]
        return last if last.isdigit() else u
    except Exception:
        return u

def textify(x: Any) -> str:
    """Return a clean string even if x is None / list / tuple / number."""
    if x is None:
        return ""
    if isinstance(x, (list, tuple)):
        parts: list[str] = []
        for v in x:
            if v is None:
                continue
            parts.append(str(v).strip())
        return " ".join(p for p in parts if p)
    # numbers, booleans, playwright JSHandles coerced to str
    return str(x).strip()

def intify(x: Any) -> Optional[int]:
    """Best-effort int; works if extractors return '56 лет', '30', ('30', 'лет'), etc."""
    if x is None:
        return None
    if isinstance(x, (list, tuple)):
        x = x[0] if x else None
    if x is None:
        return None
    m = re.search(r"\d+", str(x))
    return int(m.group(0)) if m else None


def _trim(s, n=5000): 
    return s[:n] if isinstance(s, str) else s

def scrape_open_profile(page, jd_text: str) -> dict:
    """
    Assumes we are already on a profile page after clicking from SERP.
    Scrapes fields, scores via OpenAI, returns a row for CSV.
    """
    name_raw        = extract_name_from_profile(page)
    age_raw         = extract_age_from_profile(page)
    experience_raw  = extract_experience_from_profile(page)
    about_raw       = extract_about_from_profile(page)
    education_raw   = extract_education_from_profile(page)
    recs_raw        = extract_recommendations_from_profile(page)

    # Current canonical URL:
    url_now = page.url

     # Coerce/clean
    name        = textify(name_raw)
    about       = textify(about_raw)
    education   = textify(education_raw)
    recs        = textify(recs_raw)
    age         = intify(age_raw)
    experience  = intify(experience_raw)

    score, reasons = score_with_chatgpt(
        jd_text,
        {
            "url": url_now,
            "name": name,
            "age": age,
            "experience": experience,
            "about": about,
            "education": education,
            "recommendations": recs,
        },
    )

    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "url": url_now,
        "name": name,
        "age": age,
        "experience_years": experience,
        "about": about,
        "education": education,
        "recommendations": recs,
        "score": score,
        "explanation_bullets": " • ".join(reasons),
    }

def scrape_all_profiles_on_current_serp(page, jd_text: str, *, cap: Optional[int] = None) -> int:
    """
    Iterates EVERY nanny card visible on the current SERP (no pagination here).
    For each card: open -> scrape -> append -> go back -> rebind cards.
    Returns number of rows written.
    """
    written = 0
    seen_ids_this_run: set[str] = set()  # <-- NEW

    # Count once to define the target; we always rebind before each click.
    cards = get_serp_cards(page)
    total = cards.count()
    if cap is not None:
        total = min(total, int(cap))

    print(f"[INFO] SERP shows {total} nanny cards.")
    i = 0
    while i < total:
        # Rebind the card list because DOM is destroyed after navigation
        cards = get_serp_cards(page)
        if i >= cards.count():
            break

        card = cards.nth(i)

        # try to capture href as hint
        href_hint = ""
        try:
            href_el = card.locator("a[href^='/nyanya/']").first
            if href_el.count() > 0:
                _href = href_el.get_attribute("href") or ""
                href_hint = _href if _href.startswith("http") else f"https://nashanyanya.ru{_href}"
        except Exception:
            pass

        # 1) Open the i-th profile using YOUR logic
        open_profile_from_card(page, card)

        # 2) Scrape + score
        row = scrape_open_profile(page, jd_text)
        canonical = row.get("url") or href_hint or ""
        pid = profile_id_from_url(canonical)

        # 3) Write to CSV
        if pid in seen_ids_this_run:
            print(f"[SKIP] duplicate in this run: {canonical}")
        else:
            append_row(row, OUTPUT_CSV)
            seen_ids_this_run.add(pid)
            written += 1
            print(f"[OK] [{i+1}/{total}] saved {row.get('name')!r} -> {canonical}")

        # 4) Go back to SERP (fallback to direct SERP reload if no history)
        try:
            page.go_back(wait_until="domcontentloaded")
        except Exception:
            page.goto(SERP_URL, wait_until="domcontentloaded")

        # Human-ish pacing
        page.wait_for_timeout(450 + (i % 4) * 120)

        i += 1

    return written

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

        rows_written = scrape_all_profiles_on_current_serp(page, jd_text, cap=None)  # set cap for testing
        print(f"[INFO] Wrote {rows_written} rows to {OUTPUT_CSV}")

        # (Optional) Keep window open briefly for visual check
        page.wait_for_timeout(2000)

        context.close()
        browser.close()

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
