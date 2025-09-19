# extractors.py

# --- LAST ACTIVE (RU) PARSER --------------------------------------------------
from __future__ import annotations

import os, re
from playwright.sync_api import Page, Locator, TimeoutError as PlaywrightTimeoutError
from datetime import date, datetime, timezone, timedelta
from typing import Optional, Tuple, List
import time
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

PROFILE_URL_RE = re.compile(r".*/nyanya/[^/]+/\d+/?$")
NBSP = u"\u00A0"

# Russian months (genitive case as shown on the site)
_RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

# Regexes for relative times
_RE_REL = re.compile(
    r"(?:(?P<num>\d+)\s*)?"
    r"(?P<unit>"
    r"минут(?:у|а|ы)?|"     # минуту / минута / минуты
    r"час(?:а|ов)?|"        # час / часа / часов
    r"д(?:ень|ня|ней)|"     # день / дня / дней
    r"сут(?:ки|ок)"         # сутки / суток
    r")\s*назад",
    re.IGNORECASE
)

_RE_TIME = re.compile(r"(?P<h>\d{1,2}):(?P<m>\d{2})")

# --- SERP card → absolute URL -------------------------------------------------
BASE = "https://nashanyanya.ru"



from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
import re, os

def _dismiss_blocking_overlays(page, dbg: bool = False) -> None:
    """
    Hides/removes Angular CDK overlays (coachmarks/tooltips/cookies) that can intercept clicks.
    Keeps bottom-sheets intact.
    """
    try:
        removed = page.evaluate("""
        (() => {
          const root = document.querySelector('.cdk-overlay-container');
          if (!root) return 0;
          let n = 0;

          // Any coachmark / interview / tour / tooltip panes
          for (const pane of root.querySelectorAll('.cdk-overlay-pane')) {
            // Skip the phone bottom-sheet
            if (pane.querySelector('mat-bottom-sheet-container')) continue;

            // Known blockers
            if (
              pane.querySelector('.interview') ||
              pane.querySelector('[data-tour], [data-coachmark], [role="dialog"]') ||
              pane.querySelector('.mat-tooltip') ||
              pane.querySelector('.cookie, .cookies')
            ) {
              pane.style.display = 'none';
              pane.style.pointerEvents = 'none';
              n++;
            }
          }
          // Backdrops can also steal pointer events
          for (const bd of root.querySelectorAll('.cdk-overlay-backdrop')) {
            bd.style.pointerEvents = 'none';
            n++;
          }
          return n;
        })();
        """)
        if dbg: print(f"[PHONE] overlays hidden: {removed}", flush=True)
    except Exception as e:
        if dbg: print(f"[PHONE] overlay hide error: {e}", flush=True)

def extract_phone_number(page, timeout: int = 8000) -> Optional[str]:
    dbg = os.getenv("PHONES_DEBUG") == "1"

    # 1) Click the 'Телефон' button (fast + robust)
    try:
        btn = page.locator(
            "aside .card__phone button, "
            "nn-show-resume-phone-button.card__phone button, "
            "nn-show-resume-phone-button button, "
            ".card__phone button, "
            "button:has-text('Телефон')"
        ).first

        # Make sure it really exists in the DOM and is visible (handlers attached)
        btn.wait_for(state="visible", timeout=timeout)

        # Kill smooth scrolling so any scroll is instant
        try:
            page.evaluate(
                "document.documentElement.style.scrollBehavior='auto';"
                "document.body.style.scrollBehavior='auto';"
            )
        except Exception:
            pass

        # Dismiss any overlays that could intercept clicks
        _dismiss_blocking_overlays(page, dbg=dbg)

        # Bring into viewport instantly
        try:
            btn.scroll_into_view_if_needed(timeout=800)
        except Exception:
            try:
                btn.evaluate("el => el.scrollIntoView({behavior:'auto',block:'center'})")
            except Exception:
                pass

        # Attempt 1: normal click
        if dbg: print("[PHONE] Clicking (normal)...", flush=True)
        btn.click(timeout=1500)

    except PlaywrightTimeoutError:
        if dbg: print("[PHONE] phone button not found/visible", flush=True)
        return None
    except Exception as e:
        if dbg: print(f"[PHONE] normal click error: {e}", flush=True)

    # 2) Wait for popup (with retries if needed)
    href = ""
    text = ""
    try:
        sheet = page.locator("mat-bottom-sheet-container").first
        try:
            sheet.wait_for(state="visible", timeout=1500)
        except PlaywrightTimeoutError:
            # Retry path A: JS click (bypasses hit-testing/overlays)
            try:
                if dbg: print("[PHONE] sheet not visible; retry JS el.click()", flush=True)
                btn.evaluate("el => el.click()")
                sheet.wait_for(state="visible", timeout=1500)
            except Exception:
                pass

        # If still no sheet, retry path B: force click
        if not sheet.is_visible(timeout=200):
            if dbg: print("[PHONE] sheet still not visible; retry force click", flush=True)
            _dismiss_blocking_overlays(page, dbg=dbg)
            try:
                btn.click(force=True, timeout=1200)
            except Exception as e2:
                if dbg: print(f"[PHONE] force click failed: {e2}", flush=True)

            try:
                sheet.wait_for(state="visible", timeout=2000)
            except PlaywrightTimeoutError:
                if dbg: print("[PHONE] popup or phone link not visible", flush=True)
                return None

        link = sheet.locator("a.phone").first
        link.wait_for(state="visible", timeout=1800)

        href = (link.get_attribute("href") or "").strip()
        text = (link.inner_text() or "").strip()
        if dbg: print(f"[PHONE] href='{href}' text='{text}'", flush=True)

    except PlaywrightTimeoutError:
        if dbg: print("[PHONE] popup or phone link not visible", flush=True)
        return None
    except Exception as e:
        if dbg: print(f"[PHONE] popup read error: {e}", flush=True)
        return None
    finally:
        # Try to close the popup (best-effort)
        try:
            page.locator("[data-test-id='dialog-close-button'], button[data-test-id='dialog-close-button']").first.click(timeout=1500)
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

    # --- 3) Normalize to E.164 (unchanged) ---
    raw = href if href else text
    if raw.startswith("tel:"):
        raw = raw[4:]
    digits = re.sub(r"\D", "", raw)

    if not digits:
        return None
    if len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 11 and digits.startswith("7"):
        e164 = "+" + digits
    else:
        e164 = "+" + digits
    if dbg: print(f"[PHONE] normalized -> {e164}", flush=True)
    return e164

def card_primary_url(card) -> str:
    """
    Return the absolute profile URL for a SERP card, or "" if not found.
    Works for any city: /nyanya/<city>/<id>.
    """
    a = card.locator("a[href^='/nyanya/']").first
    if a.count() == 0:
        return ""
    href = a.get_attribute("href") or ""
    return href if href.startswith("http") else f"{BASE}{href}"


# Examples the site shows (varies):
# "Была на сайте: Сейчас"
# "Был на сайте: Сегодня"
# "Была на сайте: Вчера в 11:20"
# "Был на сайте: 15 минут назад"
# "Была на сайте: 2 часа назад"
# "Был на сайте: 3 дня назад"
# "Была на сайте: 14 февраля 2025 в 09:30"
def parse_last_active_ru(raw: str, *, now: Optional[datetime] = None) -> Optional[datetime]:
    """
    Convert Russian 'last active' string to a timezone-aware datetime (local tz).
    Returns None if cannot parse confidently.

    Heuristics:
      - 'Сейчас' -> now
      - 'Сегодня' -> today (if no time, we use 'now' to guarantee <24h)
      - 'Вчера' -> yesterday (if no time, we set 12:00 to avoid off-by-one hour issues)
      - '<N> минут/часов/дней назад' -> subtract delta
      - 'DD <month> [YYYY] [в HH:MM]' -> absolute date
    """
    if not raw:
        return None

    text = raw.strip().lower().replace("ё", "е")
    now = now or datetime.now().astimezone()

    # 1) Сейчас
    if "сейчас" in text:
        return now

    # 2) Сегодня [в HH:MM]
    if "сегодня" in text:
        mt = _RE_TIME.search(text)
        if mt:
            h, m = int(mt.group("h")), int(mt.group("m"))
            return now.replace(hour=h, minute=m, second=0, microsecond=0)
        # No time given: still within 24h, return 'now'
        return now

    # 3) Вчера [в HH:MM]
    if "вчера" in text:
        mt = _RE_TIME.search(text)
        base = (now - timedelta(days=1))
        if mt:
            h, m = int(mt.group("h")), int(mt.group("m"))
            return base.replace(hour=h, minute=m, second=0, microsecond=0)
        # Noon yesterday as a reasonable center
        return base.replace(hour=12, minute=0, second=0, microsecond=0)

    # 4) Relative: "<N> ... назад"
    mrel = _RE_REL.search(text)
    if mrel:
        num_str = mrel.group("num")
        num = int(num_str) if num_str else 1  # default to 1 when number omitted
        unit = mrel.group("unit")
        unit = unit.lower()

        if unit.startswith("минут"):
            return now - timedelta(minutes=num)
        if unit.startswith("час"):
            return now - timedelta(hours=num)
        if unit.startswith("сут") or unit.startswith("д"):
            return now - timedelta(days=num)

    # 5) Absolute: "DD <месяц> [YYYY] [в HH:MM]"
    #    e.g., "14 февраля 2025 в 09:30", "3 марта в 8:00", "7 июня"
    #    Year is optional; assume current year if missing.
    abs_re = re.compile(
        r"(?P<d>\d{1,2})\s+(?P<mon>[а-я]+)(?:\s+(?P<y>\d{4}))?(?:\s+в\s+(?P<h>\d{1,2}):(?P<m>\d{2}))?",
        re.IGNORECASE
    )
    ma = abs_re.search(text)
    if ma:
        d = int(ma.group("d"))
        mon_str = ma.group("mon")
        mon = _RU_MONTHS.get(mon_str, None)
        if mon is None:
            return None
        y = int(ma.group("y")) if ma.group("y") else now.year
        h = int(ma.group("h")) if ma.group("h") else 12
        m = int(ma.group("m")) if ma.group("m") else 0
        try:
            dt = datetime(y, mon, d, h, m)
            # attach local tz
            return now.tzinfo.localize(dt) if hasattr(now.tzinfo, "localize") else dt.replace(tzinfo=now.tzinfo)
        except ValueError:
            return None

    return None

def _slice_last_active(raw: Optional[str]) -> Optional[str]:
    """Return only 'Был(а) на сайте: ...' from a longer blob."""
    if not raw:
        return None
    t = raw.replace("\xa0", " ").strip()

    # Grab the value right after the label, stop at bullet/newline
    m = re.search(r"Был[а]?\s+на\s+сайте[:\s]+(?P<val>[^•\n\r]+)", t, flags=re.IGNORECASE)
    if m:
        val = re.sub(r"\s+", " ", m.group("val")).strip(" .,:;")
        return f"Был(а) на сайте: {val}"

    # Fallback: if label is present without colon or odd spacing
    m = re.search(
        r"(Был[а]?\s+на\s+сайте)\s*(?P<val>сейчас|сегодня|вчера|[\d\s]+(?:минут|час|дн)[^•\n\r]*)",
        t, flags=re.IGNORECASE
    )
    if m:
        val = re.sub(r"\s+", " ", m.group("val")).strip(" .,:;")
        return f"Был(а) на сайте: {val}"

    return None


def extract_last_active_from_card(card, timeout: int = 1200) -> Tuple[Optional[str], Optional[datetime]]:
    """
    Read 'Был(а) на сайте: ...' from a SERP card and return (raw_text, parsed_dt).
    Never throws on absence; returns (None, None) if not found.
    """
    raw: Optional[str] = None

    # 1) Preferred: an element that contains the label
    try:
        badge = card.locator("text=/Был[а]?\\s+на\\s+сайте/i").first
        badge.wait_for(state="attached", timeout=timeout)
        blob = (badge.inner_text() or "").replace("\xa0", " ").strip()
        raw = _slice_last_active(blob)
    except Exception:
        pass

    # 2) Fallback: whole-card text → slice just the label part
    if not raw:
        try:
            blob = (card.inner_text(timeout=timeout) or "").replace("\xa0", " ").strip()
            raw = _slice_last_active(blob)
        except Exception:
            pass

    parsed = parse_last_active_ru(raw) if raw else None
    return raw, parsed


# Root selector(s) for SERP cards (both custom tag and class fallback)
CARD_SELECTOR = "nn-nanny-resume-card:visible"

def get_serp_cards(page: Page, timeout: int = 15000) -> Locator:
    """
    Return a locator for ALL nanny cards on the current SERP.
    """
    cards = page.locator(CARD_SELECTOR)
    cards.first.wait_for(state="visible", timeout=timeout)
    return cards

def go_to_next_serp_page(page: Page, timeout: int = 10000) -> bool:
    """
    Click the 'Next' paginator button and wait until the SERP actually changes.
    Returns True if navigation succeeded, False if there is no next page or it didn't change.

    We detect change by watching the primary URL of the first card.
    """
    # Snapshot the first card's URL to detect change
    try:
        cards = get_serp_cards(page)
        before_url = card_primary_url(cards.first)
    except Exception:
        before_url = ""

    # Find the Next button (enabled)
    next_btn = page.locator("button.pagination__nav_next").first
    if next_btn.count() == 0:
        return False
    try:
        # Some Angular Material buttons can be disabled via attribute/aria
        if next_btn.get_attribute("disabled") is not None:
            return False
        aria = (next_btn.get_attribute("aria-disabled") or "").lower()
        if aria in ("true", "1"):
            return False
    except Exception:
        pass

    try:
        next_btn.scroll_into_view_if_needed()
        next_btn.click()
    except Exception:
        return False

    # Wait until the first card changes (SPA pagination)
    deadline = time.time() + timeout / 1000.0
    while time.time() < deadline:
        try:
            cur_cards = get_serp_cards(page, timeout=5000)
            after_url = card_primary_url(cur_cards.first)
            if after_url and after_url != before_url:
                return True
        except Exception:
            pass
        page.wait_for_timeout(200)

    return False

def open_profile_from_card(page: Page, card: Locator, timeout: int = 15000) -> None:
    """
    Reuses your proven logic:

    1) Prefer a direct <a href^='/nyanya/moscow/'> inside the card (avatar/name).
    2) Fallback to the 'Подробнее' button.
    3) Wait for SPA to land on /nyanya/moscow/<id>, or fall back to
       presence of contact controls on the profile page.
    """
    # A) direct link (avatar or name)
    link = card.locator("a[href^='/nyanya/']").first
    if link.count() > 0:
        link.scroll_into_view_if_needed()
        link.click()
    else:
        # B) fallback: the "Подробнее" button
        more_btn = card.locator("button.button-chevron, .card-resume__more .button-chevron").first
        if more_btn.count() == 0:
            more_btn = card.get_by_text("Подробнее", exact=False).first
        more_btn.scroll_into_view_if_needed()
        more_btn.click()

    # wait for SPA url change; then fallback to profile-only UI
    try:
        page.wait_for_url(PROFILE_URL_RE, timeout=timeout)
    except PlaywrightTimeoutError:
        try:
            page.locator("text=НАПИСАТЬ, a[href^='tel:']").first.wait_for(
                state="visible", timeout=timeout
            )
        except PlaywrightTimeoutError:
            pass

def extract_name_from_profile(page, timeout=5000):
    """
    Extract nanny name from profile page.
    Strategy:
      1) h1.profile-header__title (fast, clean)
      2) fallback: <img.card__img alt="... - Имя"> => take the trailing part
      3) fallback: search in page HTML for "alt" or "name":"..."
    """
    # 1) h1 on profile
    loc = page.locator("h1.profile-header__title")
    try:
        loc.wait_for(state="visible", timeout=timeout)
        name = loc.inner_text().strip()
        if name:
            return name
    except TimeoutError:
        pass
    except PlaywrightTimeoutError:
        pass

    # 2) image alt fallback
    try:
        alt_text = page.locator("img.card__img").first.get_attribute("alt") or ""
        if alt_text:
            # Example: "Няня в городе Москва - Анжела Юрьевна А."
            parts = alt_text.split(" - ")
            if len(parts) > 1:
                return parts[-1].strip()
            return alt_text.strip()
    except Exception:
        pass

    # 3) last resort: scan HTML
    html = page.content()
    m = re.search(r'"name"\s*:\s*"([^"]+)"', html)
    if m:
        return m.group(1)

    return None

def _compute_age(birth: date, today: Optional[date] = None) -> int:
    if today is None:
        today = date.today()
    years = today.year - birth.year
    if (today.month, today.day) < (birth.month, birth.day):
        years -= 1
    return years

def extract_age_from_profile(page, timeout=3000) -> Optional[int]:
    """
    Extract age from the profile page.
    """
    html = page.content()

    # 1) Try to parse "birthDate":"..."
    m = re.search(r'"birthDate"\s*:\s*"([^"]+)"', html)
    if m:
        iso = m.group(1)
        try:
            if iso.endswith("Z"):
                dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(iso)
            return _compute_age(dt.date())
        except Exception:
            pass

    # 2) Fallback: look for "XX лет"
    try:
        body_text = page.inner_text("body", timeout=timeout)
        m2 = re.search(r'(\d{1,3})\s*лет', body_text)
        if m2:
            return int(m2.group(1))
    except Exception:
        pass

    return None

def _force_masstransit(url: str) -> str:
    """
    Ensure Yandex Maps route URL uses public transport (masstransit) instead of car.
    - Sets mode=routes and rtt=mt
    """
    if not url:
        return url
    u = urlparse(url)
    q = parse_qs(u.query, keep_blank_values=True)
    q["mode"] = ["routes"]     # make sure we land on routes UI
    q["rtt"] = ["mt"]          # 'mt' = masstransit (public transport)
    # remove any duplicates like 'auto'
    new_q = urlencode(q, doseq=True)
    return urlunparse(u._replace(query=new_q))

def parse_ru_duration_to_min(txt: str) -> Optional[int]:
    txt = (txt or "").replace("\u00a0", " ")

    # Case 1: hours + minutes, e.g. "1 ч 5 мин"
    m = re.search(r"(\d+)\s*ч(?:\D+?(\d+)\s*мин)?", txt)
    if m:
        h = int(m.group(1))
        mm = int(m.group(2)) if m.group(2) else 0
        return h * 60 + mm

    # Case 2: minutes only, e.g. "40 мин"
    m = re.search(r"(\d+)\s*мин", txt)
    if m:
        return int(m.group(1))

    # Case 3: hours only, e.g. "1 ч"
    m = re.search(r"(\d+)\s*ч\b", txt)
    if m:
        return int(m.group(1)) * 60

    return None

def _normalize_home_address(addr: str) -> str:
    s = (addr or "").strip()
    s = re.sub(r"^\s*г\.?\s*", "", s, flags=re.I)      # drop leading "г"/"г."
    s = re.sub(r"\bд\.?\s*", "", s, flags=re.I)        # drop "д "
    s = re.sub(r"\s+к\.?\s*", "к", s, flags=re.I)      # "к 2" -> "к2"
    s = re.sub(r"\s+", " ", s).strip()
    if not re.search(r"\bМосква\b", s, flags=re.I):
        s = "Москва, " + s
    return s


def extract_travel_time_via_yandex(page, home_address: str = "", timeout: int = 9000) -> Optional[int]:
    """
    Open the Yandex route in a new tab, force public transport (rtt=mt),
    route FROM nanny coords TO your home_address, read first route duration.
    Returns minutes or None. Set YAMAPS_DEBUG=1 to see step logs.
    """
    dbg = os.getenv("YAMAPS_DEBUG") == "1"
    try:
        # 1) Grab the route link on the profile
        a = page.locator('nn-map-route-link a.link, a.link.ng-star-inserted[href*="yandex.ru/maps"]').first
        a.wait_for(state="attached", timeout=4000)
        href = a.get_attribute("href") or ""
        if dbg: print(f"[YAMAPS] raw href: {href}", flush=True)
        if not href:
            return None

        # 2) Get nanny coords (prefer Google q=lat,lon; fallback rtext second point)
        n_lat = n_lon = None
        try:
            g = page.locator(".about__address .show-address__content a.show-address__link").first
            ghref = g.get_attribute("href") or ""
            if dbg: print(f"[YAMAPS] google href: {ghref}", flush=True)
            m = re.search(r"maps/\?q=([-\d\.]+),([-\d\.]+)", ghref)
            if m:
                n_lat, n_lon = map(float, m.groups())
        except Exception as e:
            if dbg: print(f"[YAMAPS] google coords failed: {e}", flush=True)

        if n_lat is None or n_lon is None:
            m = re.search(r"rtext=[^~]+~([-\d\.]+),([-\d\.]+)", href)
            if m:
                n_lat, n_lon = map(float, m.groups())
        if dbg: print(f"[YAMAPS] nanny coords: {n_lat},{n_lon}", flush=True)
        if n_lat is None or n_lon is None:
            return None

        # 3) Rewrite URL: mode=routes, rtt=mt, rtext=nanny~home; drop sticky params
        u = urlparse(href)
        q = parse_qs(u.query, keep_blank_values=True)
        q["mode"] = ["routes"]
        q["rtt"] = ["mt"]
        q.pop("ruri", None)
        q.pop("rll", None)
        q.pop("rtm", None)

        dest = _normalize_home_address(home_address) if home_address else ""
        if dest:
            q["rtext"] = [f"{n_lat},{n_lon}~{dest}"]

        new_url = urlunparse(u._replace(query=urlencode(q, doseq=True)))
        if dbg: print(f"[YAMAPS] URL => {new_url}", flush=True)

        # 4) Open and wait for PT results to render (short, bounded waits)
        tab = page.context.new_page()
        try:
            tab.goto(new_url, wait_until="domcontentloaded", timeout=timeout)
        except Exception as e:
            if dbg: print(f"[YAMAPS] goto failed: {e}", flush=True)
            try: tab.close()
            except Exception: pass
            return None

        try:
            tab.wait_for_selector(
                '[class*="masstransit-route-snippet-view__route-duration"], '
                '[class*="route-snippet-view__route-duration"]',
                timeout=min(6000, timeout),
            )
        except Exception:
            # don’t block if the snippet is slow; we’ll try body fallback
            pass

        # 5) Parse duration (no fixed sleeps)
        mins = None
        try:
            sel = (
                '[class*="masstransit-route-snippet-view__route-duration"], '
                '[class*="route-snippet-view__route-duration"]'
            )
            txt = tab.locator(sel).first.inner_text(timeout=2000)
            mins = parse_ru_duration_to_min(txt)
            if dbg: print(f"[YAMAPS] snippet text: {txt} -> {mins} min", flush=True)
        except Exception as e:
            if dbg: print(f"[YAMAPS] snippet parse failed: {e}", flush=True)

        if mins is None:
            try:
                body = tab.locator("body").inner_text(timeout=2000)
                mins = parse_ru_duration_to_min(body)
                if dbg: print(f"[YAMAPS] body fallback -> {mins} min", flush=True)
            except Exception:
                pass

        # 6) Close maps and deterministically re-sync on the profile page
        try:
            tab.close()
        finally:
            try:
                page.bring_to_front()
                page.locator("text=НАПИСАТЬ, a[href^='tel:']").first.wait_for(timeout=1200)
            except Exception:
                pass

        return mins
    except Exception as e:
        if dbg: print(f"[YAMAPS] error: {e}", flush=True)
        return None

def extract_location_from_profile(page, timeout: int = 4000) -> Optional[str]:
    """
    Reads the address text from the profile page:
    <a class="show-address__link">Москва, Калужская</a>
    Returns a cleaned string or None.
    """
    try:
        sel = ".about__address .show-address__content a.show-address__link"
        el = page.locator(sel).first
        el.wait_for(state="attached", timeout=timeout)
        txt = (el.inner_text() or "").strip()
        # collapse nbsp and extra spaces
        return " ".join(txt.replace("\xa0", " ").split()) or None
    except PlaywrightTimeoutError:
        return None
    except Exception:
        return None

def extract_experience_from_profile(page, timeout=3000) -> Optional[int]:
    """
    Years of experience (Опыт).
    1) Parse embedded JSON: "experienceAge": <int>
    2) Fallback: read the visible stat 'Лет опыта'
    Returns int or None.
    """
    html = page.content()

    # 1) JSON field
    m = re.search(r'"experienceAge"\s*:\s*(\d+)', html)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass

    # 2) Visible block: ... <div class="catalog-stats__value">12</div> <div class="catalog-stats__type">Лет опыта</div>
    try:
        xp_value_loc = page.locator(
            "xpath=//div[contains(@class,'catalog-stats__item')]"
            "[.//div[contains(@class,'catalog-stats__type')][normalize-space()='Лет опыта']]"
            "//div[contains(@class,'catalog-stats__value')][1]"
        ).first
        xp_value_loc.wait_for(state="visible", timeout=timeout)
        txt = xp_value_loc.inner_text().strip()
        # keep only digits
        digits = re.findall(r"\d+", txt)
        if digits:
            return int(digits[0])
    except Exception:
        pass

    return None


def _clean_para(s: str) -> str:
    # normalize whitespace but keep bullets/line breaks readable
    s = s.replace(NBSP, " ").replace("\r", "")
    # collapse 3+ newlines → two, and spaces around dashes
    s = re.sub(r"\n{3,}", "\n\n", s)
    # trim each line
    lines = [ln.strip() for ln in s.splitlines()]
    s = "\n".join([ln for ln in lines if ln])
    return s.strip()

def extract_about_from_profile(page, timeout=2500) -> Optional[str]:
    """
    Extract 'О себе' from profile page.
    """
    container = page.locator("div.about__content div.about__texts").first
    container.wait_for(state="visible", timeout=timeout)

    # collect all <p> elements inside
    paras = container.locator("p").all_inner_texts()
    if not paras:
        return None

    cleaned = [_clean_para(p) for p in paras if p and p.strip()]
    about_text = "\n\n".join(cleaned)
    return about_text or None


def extract_education_from_profile(page, timeout: int = 2500) -> str:
    """
    Returns the 'Образование' block as a single cleaned string.
    Falls back to locating the block by its header text.
    """
    # Primary: the footer of the education block
    candidates = [
        page.locator("nn-worker-educations .block__footer"),
        page.locator("div.block:has(h2.block__title:has-text('Образование')) .block__footer"),
    ]
    for loc in candidates:
        try:
            loc.wait_for(state="visible", timeout=timeout)
            raw = loc.inner_text().strip()
            # collapse whitespace & line breaks
            cleaned = " ".join(line.strip() for line in raw.splitlines() if line.strip())
            return cleaned
        except PlaywrightTimeoutError:
            continue
    return ""

def extract_recommendations_from_profile(page, timeout: int = 1200):
    """
    Return list[str] or None. Never blocks the crawl if the section is absent.
    """
    sel = "nn-resume-recommendation-list"
    cont = page.locator(sel).first
    try:
        cont.wait_for(state="attached", timeout=timeout)  # visible not required
    except PlaywrightTimeoutError:
        return None

    # Primary: each recommendation’s body
    items = cont.locator(".recomm__content")

    # Fallbacks for older variants (your previous logic)
    if items.count() == 0:
        items = cont.locator(".recomm__item, li, nn-resume-recommendation-item")

    n = items.count()
    if n == 0:
        return None

    out = []
    for i in range(min(n, 12)):
        try:
            t = (items.nth(i).inner_text(timeout=800) or "").strip()
            # Optional: drop leading "РЕКОМЕНДАЦИЯ" label if present
            t = re.sub(r"^\s*РЕКОМЕНДАЦИЯ\s*", "", t, flags=re.IGNORECASE)
            t = _clean_para(t)  # reuse your normalizer
            if t:
                out.append(t)
        except Exception:
            continue

    return out or None

def extract_has_audio_from_profile(page, timeout: int = 4000) -> bool:
    """
    Returns True if the profile has an audio message block/player, else False.
    Short-circuits quickly when absent to avoid multi-second stalls.
    """
    # Cap the real wait to a small value; keep signature intact for callers
    t = min(600, timeout)

    # One combined locator instead of 6 sequential waits
    sel = (
        "div.block.block_audio, "
        "text=Аудио-обращение, "
        "nn-audio-message, "
        "nn-audio-player, "
        "audio[src*='audio.nashanyanya.ru'], "
        "audio[src$='.mp3']"
    )
    try:
        loc = page.locator(sel).first
        # is_visible() returns fast; we also accept attached nodes as a positive
        if loc.is_visible(timeout=t):
            return True
        # If not visible, accept attached (lazy component still mounting)
        loc.wait_for(state="attached", timeout=t)
        return True
    except PlaywrightTimeoutError:
        return False
    except Exception:
        return False


def extract_has_fairy_tale_audio(page, timeout: int = 4000) -> bool:
    """
    Detects whether the profile has 'Записанные сказки' with an audio player.
    Short-circuits quickly when absent.
    """
    t = min(600, timeout)

    # Primary: the tales block itself
    try:
        blk = page.locator(
            "nn-voice-acting-tales, "
            "div.block:has(.block__title:has-text('Записанные сказки'))"
        ).first
        if blk.is_visible(timeout=t):
            # confirm an audio player is inside; keep this quick too
            return blk.locator("audio, nn-audio-player").first.is_visible(timeout=t)
    except Exception:
        pass

    # Fallback: any tales-hosted audio in the DOM
    try:
        return page.locator(
            "nn-voice-acting-tales audio[src*='audio.nashanyanya.ru']"
        ).first.is_visible(timeout=t)
    except Exception:
        return False
