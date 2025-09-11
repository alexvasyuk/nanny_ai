#!/usr/bin/env python3
import argparse
import os
import sys
import json
import time
from pathlib import Path

from dotenv import load_dotenv
from rich import print, box
from rich.console import Console
from rich.table import Table
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

console = Console()

# 👉 Replace these with actual selectors from the site (use Chrome DevTools → Copy selector)
SELECTORS = {
    "cookie.accept": None,           # e.g., '#cookie-accept' (optional)
    "nav.login_button": "body > nn-nanny-app > div > nn-common-header > header > div > div > div > div.header__service > nn-header-auth-state > button > span.mdc-button__label > span.m-hide",# e.g., 'a[href*=\"login\"], button:has-text(\"Войти\")'
    "login.username": "#mat-input-5",  # e.g., 'input[name=\"email\"]'
    "login.password": "#mat-input-6",  # e.g., 'input[name=\"password\"]'
    "login.submit": "body > nn-nanny-app > div > main > ng-component > form > div.login__wrap > div.login__inputs > div > button > span.mdc-button__label",    # e.g., 'button[type=\"submit\"]'
    "postlogin.marker": "body > nn-nanny-app > div > main > ng-component > div > div.layout__aside > nn-account-navigation > div > nn-user-info > div > div.user__content > div > div" # e.g., '[data-testid=\"user-avatar\"]' or text locator
}

def parse_args():
    ap = argparse.ArgumentParser(description="Login to nashanyanya.ru and save a session state.")
    ap.add_argument("--base-url", default="https://nashanyanya.ru", help="Homepage URL")
    ap.add_argument("--headless", default="false", choices=["true","false"], help="Run headless")
    ap.add_argument("--storage", default="data/session.json", help="Path to save storage state")
    ap.add_argument("--timeout", type=int, default=20000, help="Default timeout ms")
    return ap.parse_args()

def ensure_env():
    load_dotenv(override=False)
    user = os.getenv("NASH_USER")
    pw = os.getenv("NASH_PASS")
    if not user or not pw:
        console.print("[red]Missing NASH_USER or NASH_PASS in .env[/red]")
        sys.exit(1)
    return user, pw

def require_selector(name: str):
    sel = SELECTORS.get(name)
    if not sel or sel == "REPLACE_ME":
        console.print(f"[yellow]Selector missing:[/yellow] {name}. Open Chrome DevTools and fill it in nash_login.py")
        sys.exit(2)
    return sel

def main():
    args = parse_args()
    user, pw = ensure_env()
    storage_path = Path(args.storage)
    storage_path.parent.mkdir(parents=True, exist_ok=True)

    headless = args.headless.lower() == "true"
    base_url = args.base_url.rstrip("/") + "/"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        context.set_default_timeout(args.timeout)
        page = context.new_page()

        console.rule("[bold]Open page")
        page.goto(base_url, wait_until="domcontentloaded")

        # Optional cookie banner
        if SELECTORS.get("cookie.accept"):
            try:
                page.click(SELECTORS["cookie.accept"], timeout=3000)
                console.print("✅ Cookie banner closed")
            except PlaywrightTimeoutError:
                pass

        # Optional: click Login button only if selector provided
        login_btn = SELECTORS.get("nav.login_button")
        if login_btn and login_btn != "REPLACE_ME":
            console.rule("[bold]Click Login")
            try:
                page.click(login_btn)
            except PlaywrightTimeoutError:
                console.print("[yellow]Login button not clickable; continuing (already on login page?).[/yellow]")


        # Fill credentials
        uname_sel = require_selector("login.username")
        pass_sel  = require_selector("login.password")
        submit_sel= require_selector("login.submit")

        console.rule("[bold]Fill credentials")
        page.fill(uname_sel, user)
        page.fill(pass_sel, pw)
        page.click(submit_sel)

        # Wait for post-login marker
        marker = require_selector("postlogin.marker")
        try:
            console.rule("[bold]Wait for post-login marker")
            page.wait_for_selector(marker, state="visible", timeout=args.timeout)
        except PlaywrightTimeoutError:
            console.print("[red]Login may have failed (post-login marker not found). Re-check selectors/credentials.[/red]")
            sys.exit(4)

        # Save storage (cookies/localStorage)
        context.storage_state(path=str(storage_path))
        console.print(f"[green]✅ Login successful. Session saved to:[/green] {storage_path}")

        # Show a tiny report
        table = Table(title="Login Summary", box=box.SIMPLE)
        table.add_column("Key")
        table.add_column("Value")
        table.add_row("Base URL", base_url)
        table.add_row("Storage", str(storage_path))
        table.add_row("Headless", str(headless))
        console.print(table)

        browser.close()

if __name__ == "__main__":
    main()
