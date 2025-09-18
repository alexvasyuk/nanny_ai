# scorer.py
import os, re, sys, json, httpx
from openai import OpenAI
from typing import List, Tuple, Optional

from dotenv import load_dotenv
load_dotenv()  # reads .env and sets variables into os.environ

MODEL = "gpt-4.1-mini"  

def _apply_penalties_with_details(
    fit_base: int,
    age: Optional[int],
    travel_time: Optional[int],
    authenticity: Optional[float],
    has_recommendations: Optional[bool] = None,
    has_audio: Optional[bool] = None,            
    has_fairy_tale_audio: Optional[bool] = None, 
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

    if has_audio or has_fairy_tale_audio:
        add(+1, "Есть голосовое сообщение: +1")

    # Authenticity (hinged cap):
    # - No penalty at/above 0.80  → factor = 1.00
    # - Linear, mild penalty below 0.80
    #   floor = 0.60 at a=0.00, so e.g.:
    #   a=0.79 → ×0.995, a=0.60 → ×0.90, a=0.50 → ×0.85
    if authenticity is not None:
        try:
            a = max(0.0, min(1.0, float(authenticity)))
            if a >= 0.80:
                factor = 1.00
            else:
                factor = 0.60 + 0.50 * a   # line from (a=0 → 0.60) to (a=0.80 → 1.00)
            prev = score
            score = score * factor
            adjustments.append((f"Аутентичность {a:.2f} → множитель ×{factor:.2f}", score - prev))
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

def score_with_chatgpt(jd_text: str, profile: dict) -> tuple[int, list[str], bool]:
    """
    Return (score 1..10, reasons [3-5 bullets], travel_time minutes or None).
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return 0, ["error: OPENAI_API_KEY not set"], False

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
        "ЗАДАЧА: оценить профиль няни для конкретной семьи по двум осям и отдельно оценить аутентичность текста «О себе».\n\n"
        "ЖЁСТКИЕ ПРАВИЛА И СКЕПСИС:\n"
        "- Не додумывай факты: если чего-то нет в данных — укажи в missing_info.\n"
        "- Источники маркируй так: [факт] — из структурированных фактов профиля; [о себе] — из текста «О себе»; [рек.] — из рекомендаций.\n"
        "- Цитируй коротко (2–6 слов) и только по делу. Цитаты из рекомендаций помечай как [рек.]: «…».\n"
        "- Иерархия доказательств (важность по убыванию): 1) подтверждённые дела/тенюры/повторные семьи/сертификаты ([факт]/[рек.]); 2) поведенческая конкретика в «О себе» (действия, рутины, решения); 3) голые прилагательные и общие фразы.\n"
        "- КЛИШЕ («люблю детей», «ответственная», «нахожу общий язык со всеми») считаются НУЛЕВЫМ доказательством, если не поддержаны конкретикой (когда/с кем/что делала/какой результат).\n"
        "- Противоречия и несостыковки (сроки, график, возраст детей vs опыт) снижают оценки и отражаются в reasons_*.\n\n"
        "МЕТРИКИ И КРИТЕРИИ:\n"
        "- Operational fit (0–10): используй И ФАКТЫ профиля, И «О себе». Оцени: (1) возрастные группы/опыт; (2) совпадение по графику; (3) задачи/навыки; (4) безопасность/компетентность (первая помощь, рутины, план Б); (5) язык/коммуникация с семьёй. Подкрепляй каждый балл доказательствами из [факт]/[о себе]/[рек.].\n"
        "- Human fit (0–10): строго доказательно. Для каждого аспекта смотри на ДЕЙСТВИЯ/РЕЗУЛЬТАТЫ:\n"
        "  • Ответственность/надёжность — длинные тенюры, повторные семьи, «не опаздываю/предупреждаю заранее», веду дневник, план Б, CPR/первая помощь.\n"
        "  • Доброжелательность/child-centered — следую интересам ребёнка, мягкая адаптация, co-regulation/positive reinforcement, возрастно-адекватные активности.\n"
        "  • Любовь к детям — конкретные наблюдения/радости ребёнка, small extra без героики, инициативы ребёнка.\n"
        "  • Тёплая коммуникация — регулярные апдейты, ясные границы (напр. без гаджетов), согласование ожиданий с родителями.\n"
        "  Если по аспекту есть только прилагательные без примеров — считай это нулевым доказательством. Если по всей оси только общие слова — ограничь Human fit не выше 4.0/10.\n"
        "- Authenticity (0.00–1.00): оцени ТОЛЬКО «О себе» по: конкретике (возраст/примеры/рутины), первому лицу и опыту, балансу (границы/ограничения), внутренней согласованности, низкой клишированности. Несостыковки и штампы снижают балл.\n"
        "- Combined fit: вычисли как 0.6*Operational + 0.4*(Human * (0.7 + 0.3*Authenticity)).\n\n"
        "- Пол/гендер: верни is_male=true, если из данных следует мужчина (напр. «Пол: мужской», мужские формы/имя, «няня-мужчина»). Иначе false.\n\n"
        "Формат ответа — строго этот JSON:\n"
        "{\n"
        '  \"operational_fit\": <число 0..10 с 1 знаком после запятой>,\n'
        '  \"human_fit\": <число 0..10 с 1 знаком после запятой>,\n'
        '  \"authenticity\": <число 0..1 с 2 знаками после запятой>,\n'
        '  \"combined_fit\": <число 0..10 с 1 знаком после запятой>,\n'
        '  \"is_male\": <true|false>,\n'
        '  \"reasons_operational\": [\"<=5 коротких буллетов, каждый начинается с [факт]/[о себе]/[рек.] и содержит цитату\"],\n'
        '  \"reasons_human\": [\"<=5 буллетов — только доказанные маркеры поведения; если есть слабые места/клише, отметь ⚠ в конце буллета\"],\n'
        '  \"reasons_authenticity\": [\"2–4 буллета про конкретику/клише/баланс/согласованность с «цитатами»\"],\n'
        '  \"missing_info\": [\"чего не хватает (график, задачи, безопасность, рекомендации, возраст детей и т.д.)\"]\n'
        "}\n\n"
        f"Описание вакансии (JD):\n{jd_text}\n\n"
        f"Текст «О себе» няни:\n{about_text}\n\n"
        f"Факты профиля (JSON):\n{profile_summary}\n\n"
        f"Рекомендации (текст, если есть):\n{recs_text}\n"
    )


    try:
        client = make_openai_client()
        resp = client.chat.completions.create(
            model=MODEL,
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
        is_male         = bool(data.get("is_male", False))

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
        
        # NEW: pull booleans from the profile dict (use your extractor keys)
        has_audio = bool(profile.get("has_audio") or profile.get("has_audio_message"))
        has_fairy = bool(profile.get("has_fairy_tale_audio") or profile.get("has_fairy_tale"))

        # call penalties helper (add the new args)
        final_score, adjustments = _apply_penalties_with_details(
            int(round(fit_base)),
            age,
            travel_time,
            authenticity,
            has_recommendations=has_recs,
            has_audio=has_audio,                         
            has_fairy_tale_audio=has_fairy,              
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
                # show integers without .0, show halves with one decimal
                delta_str = f"{delta:+.1f}" if abs(delta % 1) >= 1e-9 else f"{int(delta):+d}"
                bullets.append(f"• {label} ({delta_str})")
        else:
            bullets.append("• нет")

        if missing_info:
            bullets.append("Чего не хватает:")
            for m in missing_info[:3]:
                bullets.append(f"• {m}")

        bullets.append(f"Итоговая оценка: {final_score}")
        return final_score, bullets, is_male


    except Exception as e:
        msg = getattr(e, "message", None) or str(e)
        code = getattr(e, "code", None)
        etype = getattr(e, "type", None)
        if code:
            print(f"[SCORER] API error ({code}): {msg}", file=sys.stderr)
            return 0, [f"error ({code}): {msg}"], False
        elif etype:
            print(f"[SCORER] API error ({etype}): {msg}", file=sys.stderr)
            return 0, [f"error ({etype}): {msg}"], False
        else:
            print(f"[SCORER] API error: {msg}", file=sys.stderr)
            return 0, [f"error: {msg}"], False

