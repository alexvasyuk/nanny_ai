#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import time
import argparse
from urllib.parse import urlparse
from typing import Any, Iterable, Optional 
from pathlib import Path
from datetime import datetime
from playwright.sync_api import sync_playwright
from scorer import score_with_chatgpt
from io_csv import append_row
from datetime import datetime, timedelta
import re
from gsheets import upsert_nannies, append_run_row, load_existing_ids
import os

from extractors import (
    get_serp_cards,
    open_profile_from_card,
    card_primary_url,
    go_to_next_serp_page, 
    # existing field extractors:
    extract_name_from_profile, 
    extract_age_from_profile,
    extract_experience_from_profile,
    extract_about_from_profile, 
    extract_education_from_profile,
    extract_recommendations_from_profile,
    extract_has_audio_from_profile,
    extract_has_fairy_tale_audio,
    extract_last_active_from_card,
    extract_location_from_profile,
    extract_travel_time_via_yandex,
    extract_phone_number,
)

# Reuse session saved by nash_login.py
STORAGE_STATE_PATH = Path("data/session.json")

# Your encoded SERP URL (with hasAudioMessage=true)
SERP_URL = 'https://nashanyanya.ru/nyanya/moscow;query=%7B%22notSmoke%22:true,%22sortOrder%22:1,%22withPhoto%22:true,%22yearExperience%22:3,%22sortBy%22:%22Activity%22,%22liveInOuts%22:%5B2%5D,%22employments%22:%5B1,2%5D,%22ageGroups%22:%5B3,4%5D,%22workingExperienceAges%22:%5B4,5,6%5D,%22hasAudioMessage%22:true%7D'

# Where to write results
OUTPUT_CSV = Path("data/nannies.csv")

JD_PATH = Path("data/jd.txt")

_ID_RE = re.compile(r"/nyanya/[^/]+/(?P<id>\d+)(?:/|$)")

def profile_id_from_url(u: str) -> str:
    """Return numeric profile id from any /nyanya/<city>/<id> URL."""
    try:
        path = urlparse(u).path
    except Exception:
        path = u or ""
    m = _ID_RE.search(path)
    return m.group("id") if m else path.rstrip("/") or u

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

def scrape_open_profile(page, jd_text: str, *, no_openai: bool = False, home_address: str = "") -> dict:
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
    location_raw    = extract_location_from_profile(page)
    travel_time     = extract_travel_time_via_yandex(page, home_address=home_address)
    phone_e164     = extract_phone_number(page, timeout=8000)

    # Current canonical URL:
    url_now = page.url

    if os.getenv("YAMAPS_DEBUG") == "1":
        print(f"[YAMAPS] yandex_tt={travel_time} for {url_now}", flush=True)  # one-line debug

     # Coerce/clean
    name        = textify(name_raw)
    about       = textify(about_raw)
    education   = textify(education_raw)
    recs        = textify(recs_raw)
    age         = intify(age_raw)
    experience  = intify(experience_raw)
    location    = textify(location_raw)

    payload = {
        "url": url_now,
        "name": name,
        "age": age,
        "experience": experience,
        "about": about,
        "education": education,
        "recommendations": recs,
        "location": location, # nanny address
        "travel_time": travel_time, # via yandex maps
    }

    if no_openai:
        score, reasons = 0, ["skipped (no-openai)"]
    else:
        score, reasons = score_with_chatgpt(
            jd_text,
            payload,
        )
        if os.getenv("SCORER_DEBUG") == "1":
            print(f"[SCORER] score={score} travel_time_min={travel_time} for {url_now}", flush=True)  # one-line debug

    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "profile_id": profile_id_from_url(url_now),
        "url": url_now,
        "profile_url": url_now,
        "name": name,
        "age": age,
        "experience_years": experience,
        "about": about,
        "education": education,
        "recommendations": recs,
        "score": score,
        "explanation_bullets": "\n".join(reasons) if reasons else "",
        "location": location,
        "travel_time_min": travel_time,
        "phone": phone_e164,
    }

def scrape_recent_on_current_serp(
    page,
    jd_text: str,
    *,
    cutoff_hours: int = 48,
    cap: Optional[int] = None,
    seen_ids: Optional[set] = None,  
    sink: Optional[list] = None, 
    no_openai: bool = False,
    home_address: str = "",       
) -> int:
    """
    Single-SERP-page workflow:
      1) COLLECT on the SERP: read last-active; keep only ≤ cutoff_hours
      2) OPEN only those candidates not in `seen_ids`
    Returns number of rows written.
    """
    cutoff_dt = datetime.now().astimezone() - timedelta(hours=cutoff_hours)
    seen_ids = seen_ids if seen_ids is not None else set()
    written = 0
    sink = sink if sink is not None else []

    # ---- 1) COLLECT (no navigation) -----------------------------------------
    cards = get_serp_cards(page)
    total = cards.count()
    print(f"[INFO] SERP shows {total} nanny cards.")

    candidates: List[Dict] = []
    for i in range(total):
        card = cards.nth(i)
        raw, last_dt = extract_last_active_from_card(card)
        url = card_primary_url(card)
        pid = profile_id_from_url(url) if url else None

        is_recent = bool(last_dt and last_dt >= cutoff_dt)
        print(
            f"[{i+1}/{total}] {url} | last_active_raw={raw!r} | "
            f"parsed={last_dt.isoformat() if last_dt else None} | recent_48h={is_recent}",
            flush=True
        )

        if not (is_recent and url):
            continue
            
        if pid and pid in seen_ids:
            if sink is not None:
                sink.append({
                    "profile_id": pid,
                    "profile_url": url,
                    "last_active_raw": raw,
                    "last_active_at": last_dt.isoformat() if last_dt else None,
                })
            print(f"[SKIP-open] known in sheet; queued last_active update: {url}", flush=True)
            written += 1            # <-- so pagination won't early-stop on this page
            continue

        candidates.append({
            "index": i,
            "url": url,
            "pid": pid,
            "last_active_raw": raw,
            "last_active_at": last_dt,
        })
        if cap is not None and len(candidates) >= cap:
            break

    print(f"[INFO] candidates ≤{cutoff_hours}h on this page: {len(candidates)}", flush=True)

    # ---- 2) OPEN candidates (navigate, scrape, write) -----------------------
    for j, c in enumerate(candidates, 1):
        page.goto(c["url"], wait_until="domcontentloaded")

        row = scrape_open_profile(page, jd_text, no_openai=no_openai, home_address=home_address)
        # audit fields from the SERP card
        row["last_active_raw"] = c["last_active_raw"]
        row["last_active_at"]  = c["last_active_at"].isoformat() if c["last_active_at"] else None

        sink.append(row)
        if c["pid"]:
            seen_ids.add(c["pid"])
        written += 1
        print(f"[OK] [{j}/{len(candidates)}] saved {row.get('name')!r} -> {row.get('url')}", flush=True)

        # Back to SERP for the next candidate
        try:
            page.go_back(wait_until="domcontentloaded")
        except Exception:
            page.goto(SERP_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(300)

    return written

def scrape_recent_across_pages(
    page,
    jd_text: str,
    *,
    cutoff_hours: int = 48,
    cap_per_page: Optional[int] = None,
    max_pages: Optional[int] = None,
    seen_ids: Optional[set] = None,  
    no_openai: bool = False,
    home_address: str = "",
) -> int:
    total_written = 0
    page_index = 1
    seen_ids = seen_ids or set()
    rows_accum: list[dict] = []     

    while True:
        print(f"\n[PAGE {page_index}] --------", flush=True)
        written_this_page = scrape_recent_on_current_serp(
            page, 
            jd_text, 
            cutoff_hours=cutoff_hours, 
            cap=cap_per_page, 
            seen_ids=seen_ids,
            sink=rows_accum,
            no_openai=no_openai,
            home_address=home_address,     
        )
        total_written += written_this_page

        # Early stop: when nothing recent left, later pages are older (sorted by date ↓)
        if written_this_page == 0:
            print("[INFO] Early stop: no ≤ cutoff candidates on this page.", flush=True)
            break

        if max_pages is not None and page_index >= max_pages:
            print(f"[INFO] Reached max_pages={max_pages}.", flush=True)
            break

        moved = go_to_next_serp_page(page, timeout=12000)
        if not moved:
            print("[INFO] No (enabled) Next or page didn't change. Done.", flush=True)
            break

        page.wait_for_timeout(350)
        page_index += 1

    return total_written, rows_accum



def main():
    t0 = time.time()

    parser = argparse.ArgumentParser()
    parser.add_argument("--sa-json", required=True, help="Path to Google service account JSON")
    parser.add_argument("--sheet-id", required=True, help="Google Sheets spreadsheet ID")
    parser.add_argument("--since-hours", type=int, default=48)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--cap-per-page", type=int, default=None)
    parser.add_argument("--new-only", action="store_true", help="Insert only new; don't update existing")
    parser.add_argument("--no-openai", action="store_true", help="Skip scoring with OpenAI")
    parser.add_argument(                                   # <-- ADD
        "--home-address",
        dest="home_address",
        default=os.getenv("HOME_ADDRESS", ""),
        help="Your address for travel-time estimate (quote if it has spaces)",
    )
    args = parser.parse_args()

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

        _, _, id_to_row, _ = load_existing_ids(args.sa_json, args.sheet_id)
        known_ids = set(id_to_row.keys())
        print(f"[SHEETS] known profiles in sheet: {len(known_ids)}")

        total_written, rows_accum = scrape_recent_across_pages(
            page,
            jd_text,
            cutoff_hours=args.since_hours,
            cap_per_page=args.cap_per_page,
            max_pages=args.max_pages,
            seen_ids=known_ids,
            no_openai=args.no_openai, 
            home_address=args.home_address,
        )
        pages_scanned = "N/A" if args.max_pages is None else args.max_pages  # set a real count if you tracked it

        # ---- UPSERT to Google Sheets ----
        new_count, upd_count = upsert_nannies(
            sa_json=args.sa_json,
            spreadsheet_id=args.sheet_id,
            scraped_rows=rows_accum,
            new_only=args.new_only,
        )
        print(f"[SHEETS] inserted={new_count} updated={upd_count}")

        # ---- Audit (optional) ----
        run_info = {
            "run_id_iso": datetime.now().astimezone().isoformat(timespec="seconds"),
            "serp_url": SERP_URL,
            "cutoff_hours": args.since_hours,
            "pages_scanned": pages_scanned,
            "candidates_scanned": len(rows_accum),
            "new_inserted": new_count,
            "updated_existing": upd_count,
            "duration_sec": round(time.time() - t0, 1),
        }
        try:
            append_run_row(args.sa_json, args.sheet_id, run_info)
        except Exception as e:
            print(f"[WARN] could not append Runs row: {e}")

        # keep session if you like, then close browser
        context.storage_state(path=STORAGE_STATE_PATH)
        browser.close()

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
