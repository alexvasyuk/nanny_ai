# scorer.py
import os, re, sys, json, httpx
from openai import OpenAI
from typing import Optional

from dotenv import load_dotenv
load_dotenv()  # reads .env and sets variables into os.environ

MODEL = "gpt-4o-mini"  # fast & cost-efficient


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

def score_with_chatgpt(jd_text: str, profile: dict, home_address: str,) -> tuple[int, list[str], Optional[int]]:
    """
    Return (score 1..10, reasons [3-5 bullets], travel_time minutes or None).
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return 0, ["error: OPENAI_API_KEY not set"], None

    client = make_openai_client()

    profile_summary = json.dumps(profile, ensure_ascii=False, indent=2)

    system_msg = (
        "Ты — строгий, прагматичный оценщик соответствия профиля вакансии. "
        "Не раскрывай ход размышлений, но предоставь краткие, предметные причины."
    )

    user_msg = (
        "Оцени соответствие профиля требованиям вакансии и верни ЧИСТЫЙ JSON строго в формате:\n"
        '{ "score": <целое 1..10>, "reasons": ["...", "...", "..."], "Travel time": <целое минуты или null> }\n'
        "Требования к reasons: 5–10 пунктов, короткие и предметные. Отрази применённые штрафы/бонусы (с числами).\n\n"

        # Консервативная оценка пути
        "Поле \"Travel time\" — консервативная оценка времени в пути от адреса няни до адреса семьи "
        "(Москва/МО) по метро/наземному, дверь-в-дверь. Будь ПЕССИМИСТИЧЕН: если сомневаешься — выбирай верхнюю границу "
        "и всегда округляй ВВЕРХ до 5 минут.\n"
        "Правила: пешком+ожидание 15–20 мин суммарно; каждая пересадка 8–10 мин; возрастной коэффициент: +15% для 50–59, +25% для 60+.\n"
        "Ориентиры: один район 25–35 мин; один радиус 35–55; через центр/1 пересадка 55–75; 2+ пересадок или край↔край 75–110; "
        "Москва↔пригород 80–130. Минимум для «противоположных концов города» — не ниже 60.\n\n"

        # Шкала влияния на итоговый SCORE
        "Скорректируй итоговый 'score' (1..10, целое) по правилам ниже. Если данных нет — штраф не применяй.\n"
        "1) AI-авторство раздела «О себе»: оцени вероятность p∈[0,1], что текст написан ИИ (клише, ровный стиль без конкретики и т.п.). "
        "Если p>0.5 — считаем ИИ и штрафуем: 0.51–0.70 → −1; 0.71–0.85 → −2; >0.85 → −3. "
        "Укажи p в reasons (например: \"О себе вероятно ИИ, p=0.78\").\n"
        "2) Время в пути (Travel time): если >60 мин, штрафуй: 61–75 → −1; 76–90 → −2; >90 → −3.\n"
        "3) Возраст: <45 → +1 (не выше 10); 45–54 → 0; 55–64 → −2; ≥65 → -3: установи верхнюю границу итогового score = 3.\n"
        "Применяй только разумные комбинации и не занижай/не завышай без оснований. Финальный score — после всех поправок.\n\n"

        "Входные данные:\n"
        f"Описание вакансии:\n{jd_text}\n\n"
        f"Адрес работодателя:\n{home_address}\n\n"
        f"Профиль няни (JSON):\n{profile_summary}\n\n"
        "Ответ — только JSON без пояснений."
    )

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            temperature=0.2,
            max_tokens=220,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()

        # Optional debug (enable by exporting SCORER_DEBUG=1)
        if os.getenv("SCORER_DEBUG") == "1":
            print(f"[SCORER] RAW:\n{raw}\n", file=sys.stderr)

        # ---- Robust JSON extraction: grab the first {...} block ----
        m = re.search(r"\{.*\}", raw, flags=re.S)
        payload = m.group(0) if m else raw
        try:
            data = json.loads(payload)
        except Exception:
            # last-ditch: strip code fences/backticks if present, then try again
            cleaned = re.sub(r"^```(?:json)?|```$", "", payload.strip(), flags=re.M)
            data = json.loads(cleaned)

        # Standard fields
        score = int(data.get("score", 0))
        reasons = data.get("reasons", [])
        if not isinstance(reasons, list):
            reasons = []
        reasons = [str(r).strip() for r in reasons if str(r).strip()]

        # Travel time: accept several key styles
        tt = (
            data.get("Travel time")
            or data.get("travel_time")
            or data.get("travelTime")
            or data.get("travel time")
        )
        if isinstance(tt, str) and tt.isdigit():
            travel_time = int(tt)
        elif isinstance(tt, (int, float)):
            travel_time = int(tt)
        else:
            travel_time = None

        score = max(1, min(10, score)) if score else 0
        if score == 0 and not reasons:
            reasons = [f"error: invalid response: {raw!r}"]
        return score, reasons, travel_time

    except Exception as e:
        msg = getattr(e, "message", None) or str(e)
        code = getattr(e, "code", None)
        etype = getattr(e, "type", None)
        if code:
            print(f"[SCORER] API error ({code}): {msg}", file=sys.stderr)
            return 0, [f"error ({code}): {msg}"], None
        elif etype:
            print(f"[SCORER] API error ({etype}): {msg}", file=sys.stderr)
            return 0, [f"error ({etype}): {msg}"], None
        else:
            print(f"[SCORER] API error: {msg}", file=sys.stderr)
            return 0, [f"error: {msg}"], None
