# scorer.py
import os, re, sys, json
from openai import OpenAI

from dotenv import load_dotenv
load_dotenv()  # reads .env and sets variables into os.environ

MODEL = "gpt-4o-mini"  # fast & cost-efficient

def score_with_chatgpt(jd_text: str, profile: dict) -> int:
    """
    Returns ONE integer 1..10. On any API issue, returns 0 and prints the reason to stderr.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("[SCORER] OPENAI_API_KEY is not set", file=sys.stderr)
        return 0

    try:
        client = OpenAI(api_key=api_key)

        profile_summary = json.dumps(profile, ensure_ascii=False, indent=2)

        system_msg = (
            "Ты — строгий оценщик соответствия профиля вакансии. "
            "Верни ОДНО целое число от 1 до 10 (без текста и пояснений). "
            "Критерии: соответствие JD и опыт; дополнительно СИЛЬНО поощряй качественное, "
            "связное и грамотно написанное описание 'О себе' на русском. Ответ только числом."
        )
        user_msg = (
            f"JOB DESCRIPTION:\n{jd_text}\n\n"
            f"PROFILE (JSON):\n{profile_summary}"
        )

        resp = client.chat.completions.create(
            model=MODEL,
            temperature=0.2,
            max_tokens=4,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\d+", raw)
        val = int(m.group()) if m else 0
        if not val:
            print(f"[SCORER] Model returned non-numeric content: {raw!r}", file=sys.stderr)
            return 0
        return max(1, min(10, val))

    except Exception as e:
        # Common causes: invalid_api_key, insufficient_quota, model_not_found, network errors, etc.
        msg = getattr(e, "message", None) or str(e)
        # Some SDK errors have .code and .type attributes:
        code = getattr(e, "code", None)
        etype = getattr(e, "type", None)
        if code:
            print(f"[SCORER] API error ({code}): {msg}", file=sys.stderr)
        elif etype:
            print(f"[SCORER] API error ({etype}): {msg}", file=sys.stderr)
        else:
            print(f"[SCORER] API error: {msg}", file=sys.stderr)
        return 0
