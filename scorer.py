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

def score_with_chatgpt(jd_text: str, profile: dict) -> tuple[int, list[str], Optional[int]]:
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
        "Требования к reasons: 3–5 пунктов, короткие и предметные.\n\n"
        "Поле \"Travel time\" — консервативная оценка времени в пути от адреса няни до адреса семьи "
        "(Москва/МО) по метро и наземному транспорту, дверь-в-дверь. Будь ПЕССИМИСТИЧЕН: "
        "если сомневаешься — выбирай верхнюю границу и всегда округляй ВВЕРХ до 5 минут.\n\n"
        "Используй такие допущения:\n"
        "• Пешком + ожидание: 15–20 мин суммарно даже при коротких маршрутах.\n"
        "• Пересадки: 8–10 мин за каждую.\n"
        "• Возрастной коэффициент: +15% к итогу для 50–59 лет, +25% для 60+.\n\n"
        "Правила диапазонов (ориентиры):\n"
        "• Один район / 1–2 остановки: 25–35 мин.\n"
        "• В пределах одного радиуса города (без сквозного пересечения центра): 35–55 мин.\n"
        "• Через центр или 1 пересадка по городу: 55–75 мин.\n"
        "• 2+ пересадки или «край города ↔ край города»: 75–110 мин.\n"
        "• Москва ↔ пригороды/МО (например: Балашиха, Химки, Одинцово, Мытищи, Реутов, Щербинка, Подольск, Люберцы, Красногорск, Видное и т.п.): 80–130 мин.\n"
        "• Минимум: если один адрес за МКАД или локации на противоположных концах города — не меньше 60 мин.\n"
        "Если данных недостаточно — верни null.\n\n"
        "Входные данные:\n"
        f"Job description:\n{jd_text}\n\n"
        f"Profile (JSON):\n{profile_summary}\n\n"
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
