# extractors.py
import re
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from datetime import date, datetime, timezone
from typing import Optional

PROFILE_URL_RE = re.compile(r"/nyanya/moscow/\d+$")

def open_first_profile_from_serp(page, timeout=15000):
    """
    On the SERP, open the first nanny profile.
    1) Click first <a href="/nyanya/moscow/..."> inside first card
    2) Fallback: click 'Подробнее' button
    Then wait for SPA URL /nyanya/moscow/<id>.
    """
    # wait for first card component
    first_card = page.locator("nn-nanny-resume-card").first
    first_card.wait_for(state="visible", timeout=timeout)

    # A) direct link (avatar or name)
    link = first_card.locator("a[href^='/nyanya/moscow/']").first
    if link.count() > 0:
        link.scroll_into_view_if_needed()
        link.click()
    else:
        # B) fallback: the "Подробнее" button
        more_btn = first_card.locator("button.button-chevron, .card-resume__more .button-chevron").first
        if more_btn.count() == 0:
            more_btn = first_card.get_by_text("Подробнее", exact=False).first
        more_btn.scroll_into_view_if_needed()
        more_btn.click()

    # wait for SPA url change; then fallback to profile-only UI
    try:
        page.wait_for_url(PROFILE_URL_RE, timeout=timeout)
    except PlaywrightTimeoutError:
        try:
            page.locator("text=НАПИСАТЬ, a[href^='tel:']").first.wait_for(state="visible", timeout=timeout)
        except PlaywrightTimeoutError:
            pass
    return page


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
