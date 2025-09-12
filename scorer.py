# scorer.py
import os, re, sys, json
from openai import OpenAI

from dotenv import load_dotenv
load_dotenv()  # reads .env and sets variables into os.environ

MODEL = "gpt-4o-mini"  # fast & cost-efficient

def score_with_chatgpt(jd_text: str, profile: dict) -> tuple[int, list[str]]:
    """
    Return (score 1..10, reasons [3-5 bullets]).
    On error, returns (0, ["error: ..."]).
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return 0, ["error: OPENAI_API_KEY not set"]

    client = OpenAI(api_key=api_key)

    profile_summary = json.dumps(profile, ensure_ascii=False, indent=2)

    system_msg = (
        "Ты — строгий, прагматичный оценщик соответствия профиля вакансии. "
        "Не раскрывай ход размышлений, но предоставь краткие, предметные причины."
    )
    user_msg = (
        "Оцени соответствие профиля требованиям вакансии и верни ЧИСТЫЙ JSON:\n"
        '{ "score": <целое 1..10>, "reasons": ["...", "...", "..."] }\n'
        "Требования к reasons: 3–5 пунктов, короткие и содержательные; отражают ключевые факторы "
        "(соответствие JD, опыт, образование, расписание/формат работы, рекомендации, качество описания «О себе», "
        "наличие аудио и др.). Без воды и без повторов. Только JSON, без пояснительного текста.\n\n"
        f"JOB DESCRIPTION:\n{jd_text}\n\n"
        f"PROFILE (JSON):\n{profile_summary}"
    )

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            temperature=0.2,
            max_tokens=220,   # a bit higher for 3–5 bullets
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()

        # Parse JSON strictly; fall back if needed
        try:
            data = json.loads(raw)
            score = int(data.get("score", 0))
            reasons = data.get("reasons", [])
            if not isinstance(reasons, list):
                reasons = []
            reasons = [str(r).strip() for r in reasons if str(r).strip()]
        except Exception:
            # Fallback: take a number + split lines as bullets if JSON fails
            m = re.search(r"\b([1-9]|10)\b", raw)
            score = int(m.group(1)) if m else 0
            # naive bullets fallback (split by line)
            reasons = [line.strip("-•– \t") for line in raw.splitlines() if line.strip()][:5]

        score = max(1, min(10, score)) if score else 0
        if score == 0 and not reasons:
            reasons = [f"error: invalid response: {raw!r}"]
        return score, reasons

    except Exception as e:  # Common causes: invalid_api_key, insufficient_quota, model_not_found, network errors, etc.
        msg = getattr(e, "message", None) or str(e)
        # Some SDK errors have .code and .type attributes:
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

