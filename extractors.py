# extractors.py
import re
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

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
