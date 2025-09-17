# scorer.py
import os, re, sys, json, httpx
from openai import OpenAI
from typing import List, Tuple, Optional

from dotenv import load_dotenv
load_dotenv()  # reads .env and sets variables into os.environ

MODEL = "gpt-5"  

def _apply_penalties_with_details(
    fit_base: int,
    age: Optional[int],
    travel_time: Optional[int],
    authenticity: Optional[float],
    has_recommendations: Optional[bool] = None,
) -> tuple[int, list[tuple[str, int]]]:
    """
    Deterministic adjustments + a multiplicative authenticity cap.
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
            add(-1, "Возраст 55–64: -1")
        elif age >= 65:
            add(-2, "Возраст ≥ 65: -2 (потолок 3)")
            cap_to_3 = True

    # Travel time
    if travel_time is not None and travel_time > 60:
        if 61 <= travel_time <= 75:
            add(-1, "Время в пути 61–75 мин: -1")
        elif 76 <= travel_time <= 90:
            add(-2, "Время в пути 76–90 мин: -2")
        else:
            add(-3, "Время в пути > 90 мин: -3")
    
    # NEW: recommendations bonus (+1 if any)
    if has_recommendations:
        add(+1, "Есть рекомендации: +1")

    # Authenticity (separate lever, multiplicative cap 0.50..1.00)
    if authenticity is not None:
        try:
            a = max(0.0, min(1.0, float(authenticity)))
            factor = 0.5 + 0.5 * a               # 0.50..1.00
            new_score = int(round(score * factor))
            delta = new_score - score
            adjustments.append((f"Аутентичность {a:.2f} → множитель ×{factor:.2f}", delta))
            score = new_score
        except Exception:
            pass

    # Clamp 1..10 and 65+ cap
    score = max(1, min(10, score))
    if cap_to_3:
        score = min(score, 3)
    return score, adjustments

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
    about_text = (profile.get("about") or profile.get("about_me") or profile.get("description") or "").strip()
    recs = profile.get("recommendations") or []
    recs_text = "\n".join(f"- {r.strip()}" for r in recs[:5]) or "—"


    system_msg = (
        "Ты — строгий, прагматичный оценщик соответствия профиля вакансии. "
        "Не раскрывай ход размышлений, верни только JSON."
    )

    user_msg = (
        "Верни СТРОГО ЧИСТЫЙ JSON (без пояснений и текста вне JSON).\n\n"
        "Твоя задача: оценить профиль няни для конкретной семьи по двум осям и дать отдельную оценку аутентичности текста «О себе».\n"
        "Правила:\n"
        "- Не додумывай факты: если чего-то нет в данных — укажи в missing_info.\n"
        "- Все причины подкрепляй короткими цитатами (2–6 слов) из «О себе» ИЛИ из текста рекомендаций. Если цитата из рекомендации, пометь её как [рек.]: «…». \n"
        "- Operational fit (0–10): используй И ФАКТЫ профиля, И текст «О себе». Оцени: (1) возрастные группы/опыт, (2) совпадение по графику, (3) задачи/скиллы, (4) безопасность/компетентность, (5) язык/коммуникация с семьёй.\n"
        "- Human fit (0–10): опирайся на «О себе», но можешь учитывать объективные маркеры из фактов (длительные тенуры, повторные семьи, рекомендации, сертификаты). Оцени: ответственность/надёжность, доброжелательность/child-centered, любовь к детям, тёплая коммуникация.\n"
        "- Authenticity (0.00–1.00): оцени ТОЛЬКО текст «О себе» по конкретике (возраст/примеры/рутины), первому лицу и опыту, балансу (границы/ограничения), внутренней согласованности и низкой клишированности.\n"
        "- Combined fit (до коррекций) = 0.6*Operational + 0.4*Human.\n\n"
        "Формат ответа — только этот JSON:\n"
        "{\n"
        '  "operational_fit": <число 0..10 с 1 знаком после запятой>,\n'
        '  "human_fit": <число 0..10 с 1 знаком после запятой>,\n'
        '  "authenticity": <число 0..1 с 2 знаками после запятой>,\n'
        '  "combined_fit": <число 0..10 с 1 знаком после запятой>,\n'
        '  "reasons_operational": ["<=5 коротких буллетов с «цитатами»"],\n'
        '  "reasons_human": ["<=5 коротких буллетов с «цитатами»"],\n'
        '  "reasons_authenticity": ["2–4 буллета про конкретику/клише/баланс с «цитатами»"],\n'
        '  "missing_info": ["чего не хватает (график, задачи, рекомендации и т.д.)"]\n'
        "}\n\n"
        f"Описание вакансии (JD):\n{jd_text}\n\n"
        f"Текст «О себе» няни:\n{about_text}\n\n"
        f"Факты профиля (JSON):\n{profile_summary}\n"
        f"Рекомендации (текст, если есть):\n{recs_text}\n\n"
    )


    try:
        client = make_openai_client()
        resp = client.chat.completions.create(
            model=MODEL,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
        )
        content = resp.choices[0].message.content
        data = _safe_json_load(content)

        operational_fit = float(data.get("operational_fit", 0.0))
        human_fit       = float(data.get("human_fit", 0.0))
        authenticity    = float(data.get("authenticity", 0.5))
        combined_fit    = float(data.get("combined_fit", 0.0)) or (0.6*operational_fit + 0.4*human_fit)

        reasons_operational   = data.get("reasons_operational", []) or []
        reasons_human         = data.get("reasons_human", []) or []
        reasons_authenticity  = data.get("reasons_authenticity", []) or []
        missing_info          = data.get("missing_info", []) or []

        # Базовая оценка, которая уйдёт в корректировки
        fit_base = max(0.0, min(10.0, combined_fit))

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
    
        recs = profile.get("recommendations") or profile.get("recs") or []
        if isinstance(recs, (list, tuple)):
            has_recs = len(recs) > 0
        elif isinstance(recs, str):
            has_recs = bool(recs.strip())
        else:
            has_recs = False
        
        final_score, adjustments = _apply_penalties_with_details(
            int(round(fit_base)),
            age,
            travel_time,
            authenticity,
            has_recommendations=has_recs,   # <-- NEW
        )

        # Build structured bullets:
        bullets: list[str] = []

        bullets.append(f"Операционный фит: {operational_fit:.1f}")
        for r in reasons_operational[:4]:
            bullets.append(f"• {r}")

        bullets.append(f"Человеческий фит: {human_fit:.1f}")
        for r in reasons_human[:4]:
            bullets.append(f"• {r}")

        bullets.append(f"Аутентичность «О себе»: {authenticity:.2f}")
        for r in reasons_authenticity[:3]:
            bullets.append(f"• {r}")

        bullets.append(f"Комбинированный (до корректировок): {fit_base:.1f}")

        bullets.append("Корректировки:")
        if adjustments:
            for label, delta in adjustments:
                bullets.append(f"• {label} ({delta:+d})")
        else:
            bullets.append("• нет")

        if missing_info:
            bullets.append("Чего не хватает:")
            for m in missing_info[:3]:
                bullets.append(f"• {m}")

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

