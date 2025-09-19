#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse, os, sys, time, traceback
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from urllib.parse import urlparse

from gsheets import load_existing_ids, upsert_nannies
from scorer import score_with_chatgpt  # uses your new skeptical prompt

def read_text(p: str) -> str:
    return Path(p).read_text(encoding="utf-8").strip()

def _mask_url(u: str) -> str:
    try:
        p = urlparse(u)
        host = p.hostname or ""
        port = (":" + str(p.port)) if p.port else ""
        scheme = (p.scheme + "://") if p.scheme else ""
        return f"{scheme}{host}{port}" if host else "<custom>"
    except Exception:
        return "<custom>"

def print_net_env():
    keys = [
        "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_PROXY",
        "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy"
    ]
    print("[RESCORE] Network env:", flush=True)
    for k in keys:
        v = os.environ.get(k)
        if not v:
            print(f"  - {k}: <unset>", flush=True)
        else:
            print(f"  - {k}: set -> {_mask_url(v)}", flush=True)

def call_with_timeout(jd_text, row_dict, seconds, idx_info=""):
    # Run score_with_chatgpt in a thread so we can enforce a timeout
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(score_with_chatgpt, jd_text, row_dict)
        return fut.result(timeout=seconds)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sa-json", required=True)
    ap.add_argument("--sheet-id", required=True)
    ap.add_argument("--jd-file", required=True)
    ap.add_argument("--only-missing", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=int, default=60, help="Per-profile ChatGPT timeout (sec)")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="Do everything except write updates to the sheet")
    args = ap.parse_args()

    t0 = time.time()
    print(f"[RESCORE] Start  sheet={args.sheet_id}  only_missing={args.only_missing}  "
          f"limit={args.limit}  timeout={args.timeout}s  dry_run={args.dry_run}", flush=True)
    print_net_env()

    # Load JD
    jd_text = read_text(args.jd_file)
    if args.verbose:
        print(f"[RESCORE] JD loaded ({len(jd_text)} chars)", flush=True)

    # Load existing sheet
    t = time.time()
    print("[RESCORE] Loading sheet metadata…", flush=True)
    ws, header_map, id_to_row, existing_urls_by_id = load_existing_ids(args.sa_json, args.sheet_id)
    if args.verbose:
        print(f"[RESCORE] Header keys: {sorted(list(header_map.keys()))}", flush=True)

    print("[RESCORE] Fetching all records from sheet…", flush=True)
    rows = ws.get_all_records()  # may take a while on big sheets
    print(f"[RESCORE] Rows fetched: {len(rows)}  ({time.time()-t:.1f}s)", flush=True)

    # Select candidates
    candidates = []
    for r in rows:
        pid = str(r.get("profile_id") or "").strip()
        if not pid:
            continue
        about = (r.get("about") or r.get("about_me") or r.get("description") or "").strip()
        if not about:
            continue

        if args.only_missing:
            has_score = str(r.get("score", "")).strip() != ""
            has_bullets = bool((r.get("explanation_bullets") or "").strip())
            if has_score and has_bullets:
                continue

        candidates.append(r)

    total = len(candidates)
    if args.limit and total > args.limit:
        candidates = candidates[:args.limit]
        total = len(candidates)

    print(f"[RESCORE] Will process: {total} row(s).", flush=True)

    # Score loop
    to_update = []
    for i, r in enumerate(candidates, 1):
        pid = str(r.get("profile_id") or "").strip() or "<noid>"
        about_len = len((r.get("about") or r.get("about_me") or r.get("description") or ""))

        print(f"[{i}/{total}] Scoring pid={pid} about_len={about_len}…", flush=True)
        t1 = time.time()
        try:
            score, bullets, is_male = call_with_timeout(jd_text, r, args.timeout, idx_info=f"{i}/{total}")
        except FuturesTimeout:
            print(f"  ↳ TIMEOUT after {args.timeout}s — skipping pid={pid}", flush=True)
            continue
        except KeyboardInterrupt:
            print("\n[RESCORE] Interrupted by user.", flush=True)
            return 130
        except Exception as e:
            print(f"  ↳ ERROR pid={pid}: {e}", flush=True)
            if args.verbose:
                traceback.print_exc()
            continue

        dur = time.time() - t1
        bullets = bullets or []
        print(f"  ↳ done in {dur:.1f}s  score={score}  bullets={len(bullets)}", flush=True)

        to_update.append({
            "profile_id": pid,
            "profile_url": r.get("profile_url") or r.get("url") or "",
            "score": score,
            "explanation_bullets": "\n".join([b for b in bullets if b]).strip()
        })

    # Update
    print(f"[RESCORE] Prepared updates: {len(to_update)}", flush=True)
    if not to_update:
        print("[RESCORE] Nothing to update. Exiting.", flush=True)
        return 0

    if args.dry_run:
        print("[RESCORE] Dry run enabled — not writing to the sheet.", flush=True)
        print(f"[RESCORE] Total runtime: {time.time()-t0:.1f}s", flush=True)
        return 0

    print("[RESCORE] Writing updates to sheet…", flush=True)
    try:
        new_count, upd_count = upsert_nannies(
            sa_json=args.sa_json,
            spreadsheet_id=args.sheet_id,
            scraped_rows=to_update,
            new_only=False,  # update existing rows
        )
        print(f"[RESCORE] upsert_nannies -> new={new_count} updated={upd_count}", flush=True)
    except Exception as e:
        print(f"[RESCORE] ERROR during sheet update: {e}", flush=True)
        if args.verbose:
            traceback.print_exc()

    print(f"[RESCORE] Done in {time.time()-t0:.1f}s", flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
