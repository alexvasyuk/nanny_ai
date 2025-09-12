# extractors.py
import re
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from datetime import date, datetime, timezone
from typing import Optional, Tuple, List

PROFILE_URL_RE = re.compile(r"/nyanya/moscow/\d+$")
NBSP = u"\u00A0"


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

def extract_about_from_profile(page, timeout=6000) -> Optional[str]:
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


def extract_education_from_profile(page, timeout: int = 6000) -> str:
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

def extract_recommendations_from_profile(page, timeout: int = 6000) -> Tuple[int, List[str]]:
    """
    Returns (count, texts[]) for the 'Мои рекомендатели' block.
    Robust to the block being collapsed.
    """
    container = page.locator("nn-resume-recommendation-list")
    container.wait_for(state="visible", timeout=timeout)

    # Expand if there's a "Показать/Скрыть" toggle
    try:
        toggle = container.locator(".recomm__more .button-toggle")
        if toggle.is_visible():
            toggle.click()
    except Exception:
        pass  # not critical

    items = container.locator(".recomm__item")
    n = items.count()

    texts: List[str] = []
    for i in range(n):
        # each item has .recomm__content (the full recommendation text)
        txt = items.nth(i).locator(".recomm__content").inner_text(timeout=timeout).strip()
        if txt:
            # normalize whitespace a bit
            texts.append(" ".join(txt.split()))

    return n, texts

def extract_has_audio_from_profile(page, timeout: int = 4000) -> bool:
    """
    Returns True if the profile has an audio message block/player, else False.
    Works even if Angular-generated ids/classes vary.
    """
    candidate_selectors = [
        "div.block.block_audio",                    # wrapper block
        "text=Аудио-обращение",                     # header text
        "nn-audio-message",                         # component tag
        "nn-audio-player",                          # player wrapper
        "audio[src*='audio.nashanyanya.ru']",       # concrete audio host
        "audio[src$='.mp3']"                        # fallback: any mp3
    ]

    for sel in candidate_selectors:
        try:
            node = page.locator(sel).first
            node.wait_for(state="visible", timeout=timeout)
            return True
        except PlaywrightTimeoutError:
            continue

    return False