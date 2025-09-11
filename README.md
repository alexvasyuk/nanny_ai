# Nashanyanya.ru — Playwright login (starter)

This minimal script opens the homepage, clicks the login button, signs in, and saves a session file you can reuse.

## 1) Setup

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install
```

Create `.env` from the example and fill your credentials:

```bash
cp .env.example .env
# edit .env to set NASH_USER and NASH_PASS
```

## 2) Run (visible browser)

```bash
python nash_login.py --base-url "https://nashanyanya.ru/" --headless false
```

Run headless:

```bash
python nash_login.py --headless true
```

The script will save a session to `data/session.json` if login succeeds.

## 3) Fill selectors

Open the site in Chrome, right-click the element → Inspect → right-click in DevTools → Copy → Copy selector.
Then paste into `SELECTORS` in `nash_login.py`:

- `nav.login_button` — the "Login" or "Sign in" button on the homepage.
- `login.username` — the username/email/phone input on the login form.
- `login.password` — the password input on the login form.
- `login.submit` — the submit button on the login form.
- `postlogin.marker` — an element that only exists after login (e.g., avatar/menu).

If `Copy selector` looks brittle, prefer text-based locators, e.g. `page.get_by_role("button", name="Войти")`.

## 4) Troubleshooting

- If a cookie banner blocks interactions, add a selector to close it before clicking login.
- If the site uses phone/SMS login, adapt the script to fill the phone field and handle a code step (manual pause can be added).
- Use `--timeout 30000` if your connection is slow.
