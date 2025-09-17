# scorer.py
import os, re, sys, json, httpx
from openai import OpenAI
from typing import List, Tuple, Optional

from dotenv import load_dotenv
load_dotenv()  # reads .env and sets variables into os.environ

MODEL = "gpt-4o-mini"  # fast & cost-efficient

def _apply_penalties_with_details(
    fit_base: int,
    age: Optional[int],
    travel_time: Optional[int],
    ai_p: Optional[float],
) -> tuple[int, list[tuple[str, int]]]:
    """
    Same rules as _apply_penalties, but also returns a detailed adjustments log.
    Returns: (final_score, adjustments) where adjustments = [(label, delta), ...]
    - Clamp 1..10
    - 65+ cap to 3 is applied last (after clamp).
    """
    score = int(fit_base)
    adjustments: list[tuple[str, int]] = []
    def add(delta: int, label: str):
        nonlocal score
        if not delta:
            return
        score += int(delta)
        adjustments.append((label, int(delta)))

    # Age
    cap_to_3 = False
    if age is not None:
        if age < 45:
            add(+1, "Возраст < 45: +1")
        elif 55 <= age <= 64:
            add(-2, "Возраст 55–64: -1")
        elif age >= 65:
            add(-3, "Возраст ≥ 65: -2 (потолок 3)")
            cap_to_3 = True

    # Travel time
    if travel_time is not None and travel_time > 60:
        if travel_time <= 75:
            add(-1, "Время в пути 61–75 мин: -1")
        elif travel_time <= 90:
            add(-2, "Время в пути 76–90 мин: -2")
        else:
            add(-3, "Время в пути > 90 мин: -3")

    # AI prob
    if ai_p is not None and ai_p > 0.50:
        if ai_p <= 0.70:
            add(-1, f"«О себе» похоже на ИИ (p={ai_p:.2f}): -1")
        elif ai_p <= 0.85:
            add(-2, f"«О себе» похоже на ИИ (p={ai_p:.2f}): -2")
        else:
            add(-3, f"«О себе» похоже на ИИ (p={ai_p:.2f}): -3")

    # Clamp 1..10
    score = max(1, min(10, score))
    if cap_to_3:
        score = min(score, 3)
    return score, adjustments

def _extract_ai_prob(reasons: List[str]) -> Optional[float]:
    """
    Tries to find 'p=0.78' (or similar) in reasons and return it as float.
    Returns None if not found.
    """
    for r in reasons or []:
        m = re.search(r"p\s*=\s*([0-9]*\.?[0-9]+)", r)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None

def _safe_json_load(s: str) -> dict:
    """Parse JSON; if model wrapped it with text, grab the first {...} block."""
    import re, json
    try:
        return json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s, flags=re.S)
        if m:
            return json.loads(m.group(0))
        raise


def make_openai_client() -> OpenAI:
    proxy = os.getenv("OPENAI_PROXY")  # e.g. socks5://127.0.0.1:1080  OR socks5h://…
    if not proxy:
        return OpenAI()

    scheme = proxy.split(":", 1)[0].lower()

    if scheme.startswith("socks5"):                   # accept socks5 and socks5h
        # normalize for httpx-socks/python-socks
        proxy_url = proxy.replace("socks5h://", "socks5://", 1)
        from httpx_socks import SyncProxyTransport    # pip install httpx-socks
        transport = SyncProxyTransport.from_url(proxy_url)  # rdns=True by default
        http_client = httpx.Client(transport=transport, timeout=60)
    else:                                             # HTTP/HTTPS proxy
        http_client = httpx.Client(
            transport=httpx.HTTPTransport(proxy=proxy),
            timeout=60,
        )

    return OpenAI(http_client=http_client)

from typing import Optional
import os, sys, re, json

def score_with_chatgpt(jd_text: str, profile: dict) -> tuple[int, list[str]]:
    """
    Return (score 1..10, reasons [3-5 bullets], travel_time minutes or None).
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return 0, ["error: OPENAI_API_KEY not set"], None

    profile_summary = json.dumps(profile, ensure_ascii=False, indent=2)

    system_msg = (
        "Ты — строгий, прагматичный оценщик соответствия профиля вакансии. "
        "Не раскрывай ход размышлений, верни только JSON."
    )

    user_msg = (
        "Верни ЧИСТЫЙ JSON:\n"
        '{ "fit_base": <целое 1..10>, "ai_about_prob": <число 0..1>, "reasons": ["...", "..."] }\n\n'
        "Правила:\n"
        "- fit_base оценивает только соответствие профиля требованиям вакансии (НЕ учитывай возраст, время в пути и штрафы).\n"
        "- ai_about_prob: оцени вероятность, что раздел «О себе» написан ИИ, по признакам (клише, ровный стиль без деталей, "
        "шаблонные списки, отсутствие опечаток и т.п.).\n"
        "  Выбирай одно из значений: 0.50, 0.70, 0.85, 0.95 (края диапазонов ≤0.50 / 0.51–0.70 / 0.71–0.85 / >0.85).\n"
        "  Если «О себе» отсутствует или данных мало — 0.50.\n"
        "- reasons: 5–8 коротких пунктов; укажи выбранный диапазон для ai_about_prob словами (например: "
        "«О себе похоже на ИИ (диапазон 0.71–0.85)»).\n\n"
        "Ответ — только JSON.\n\n"
        f"Описание вакансии:\n{jd_text}\n\n"
        f"Профиль няни (JSON):\n{profile_summary}\n\n"
    )

    try:
        client = make_openai_client()
        resp = client.chat.completions.create(
            model=MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
        )
        content = resp.choices[0].message.content
        data = _safe_json_load(content)

        fit_base = int(data.get("fit_base", 1))
        ai_p = float(data.get("ai_about_prob", 0.5))
        reasons = data.get("reasons", [])
        if not isinstance(reasons, list):
            reasons = [str(reasons)]

        # Pull age and travel_time from profile
        age = profile.get("age")
        try:
            age = int(age) if age is not None else None
        except Exception:
            age = None

        travel_time = profile.get("travel_time") or profile.get("travel_time_min")
        try:
            travel_time = int(travel_time) if travel_time is not None else None
        except Exception:
            travel_time = None
    
        final_score, adjustments = _apply_penalties_with_details(fit_base, age, travel_time, ai_p)

        # Build structured bullets:
        bullets: list[str] = []
        bullets.append(f"Базовая оценка: {fit_base}")
        for r in reasons[:8]:  # оставляем до 8, обычно 4–7
            bullets.append(f"• {r}")

        bullets.append("Корректировки:")
        if adjustments:
            for label, delta in adjustments:
                bullets.append(f"• {label} ({delta:+d})")
        else:
            bullets.append("• нет")

        bullets.append(f"Итоговая оценка: {final_score}")

        return final_score, bullets

    except Exception as e:
        msg = getattr(e, "message", None) or str(e)
        code = getattr(e, "code", None)
        etype = getattr(e, "type", None)
        if code:
            print(f"[SCORER] API error ({code}): {msg}", file=sys.stderr)
            return 0, [f"error ({code}): {msg}"]
        elif etype:
            print(f"[SCORER] API error ({etype}): {msg}", file=sys.stderr)
            return 0, [f"error ({etype}): {msg}"]
        else:
            print(f"[SCORER] API error: {msg}", file=sys.stderr)
            return 0, [f"error: {msg}"]

