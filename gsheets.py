# gsheets.py
from __future__ import annotations

import datetime as dt
from typing import Dict, List, Tuple, Optional

import gspread
from gspread.cell import Cell

# ------------------------- Sheet & headers -------------------------

NANNIES_SHEET = "Nannies"
RUNS_SHEET    = "Runs"

# Columns we write on insert
NANNIES_HEADERS: List[str] = [
    "profile_id",
    "profile_url",
    "name",
    "city",
    "age",
    "experience_years",
    "about",
    "education",
    "recommendations",
    "score",
    "explanation_bullets",
    "last_active_raw",
    "last_active_at",
    "first_seen_at",
    "last_seen_at",
    # Human-editable (we DON'T overwrite these in updates)
    "status",
    "owner",
    "notes",
    "last_contacted_at",
]

# For updates on existing rows, we ONLY touch these:
MACHINE_UPDATE_COLS = [
    "last_active_raw",
    "last_active_at",
    "last_seen_at",
    # optionally keep score fresh—include the two below if you want that
    # "score",
    # "explanation_bullets",
]

def _client(sa_json: str) -> gspread.Client:
    return gspread.service_account(filename=sa_json)

def _open_sheet(sa_json: str, spreadsheet_id: str) -> gspread.Spreadsheet:
    gc = _client(sa_json)
    return gc.open_by_key(spreadsheet_id)

def _get_or_create_ws(sh: gspread.Spreadsheet, title: str) -> gspread.Worksheet:
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=1000, cols=40)

def _ensure_headers(ws: gspread.Worksheet, headers: List[str]) -> Dict[str, int]:
    """Ensure first row == headers; return {col_name: 1-based index}."""
    existing = ws.row_values(1)
    if existing != headers:
        # set the header row exactly
        ws.update("A1", [headers])
        try:
            ws.freeze(rows=1)
        except Exception:
            pass
    return {name: i + 1 for i, name in enumerate(headers)}

# ------------------------- Read existing IDs -------------------------

def load_existing_ids(sa_json: str, spreadsheet_id: str) -> Tuple[gspread.Worksheet, Dict[str, int], Dict[str, int]]:
    """
    Return (worksheet, header_map, id_to_row).
    id_to_row maps profile_id -> row_index (2-based and beyond).
    """
    sh = _open_sheet(sa_json, spreadsheet_id)
    ws = _get_or_create_ws(sh, NANNIES_SHEET)
    header_map = _ensure_headers(ws, NANNIES_HEADERS)

    # read the profile_id column
    id_col = header_map["profile_id"]
    col_vals = ws.col_values(id_col)  # includes header at [0]
    id_to_row: Dict[str, int] = {}
    for i, val in enumerate(col_vals[1:], start=2):  # start at row 2
        if val:
            id_to_row[val] = i
    return ws, header_map, id_to_row

# ------------------------- Append NEW rows -------------------------

def append_new_rows(ws: gspread.Worksheet, header_map: Dict[str, int], rows: List[dict]) -> int:
    """Append brand-new rows to the bottom. rows must already have keys matching NANNIES_HEADERS."""
    if not rows:
        return 0
    payload = []
    for r in rows:
        payload.append([r.get(h, "") for h in NANNIES_HEADERS])
    # USER_ENTERED allows date-like strings to be displayed nicely
    ws.append_rows(payload, value_input_option="USER_ENTERED")
    return len(rows)

# ------------------------- Update EXISTING rows (partial) -------------------------

def batch_update_machine_fields(
    ws: gspread.Worksheet,
    header_map: Dict[str, int],
    updates: Dict[int, dict],  # row_index -> partial row dict
) -> int:
    """
    Update only MACHINE_UPDATE_COLS for the given rows (by row index).
    Returns number of rows that had at least one cell updated.
    """
    if not updates:
        return 0
    cells: List[Cell] = []
    rows_touched = 0

    for row_idx, data in updates.items():
        touched_this_row = False
        for col_name in MACHINE_UPDATE_COLS:
            if col_name not in header_map:
                continue
            if col_name not in data:
                continue
            col_idx = header_map[col_name]
            val = data[col_name]
            if val is None:
                val = ""
            cells.append(Cell(row=row_idx, col=col_idx, value=str(val)))
            touched_this_row = True
        if touched_this_row:
            rows_touched += 1

    if cells:
        ws.update_cells(cells, value_input_option="USER_ENTERED")
    return rows_touched

# ------------------------- Runs sheet -------------------------

def append_run_row(sa_json: str, spreadsheet_id: str, run_info: dict) -> None:
    """Append a single audit row to the 'Runs' sheet."""
    sh = _open_sheet(sa_json, spreadsheet_id)
    ws = _get_or_create_ws(sh, RUNS_SHEET)

    headers = [
        "run_id_iso",
        "serp_url",
        "cutoff_hours",
        "pages_scanned",
        "candidates_scanned",
        "new_inserted",
        "updated_existing",
        "duration_sec",
    ]
    _ensure_headers(ws, headers)
    ws.append_row(
        [run_info.get(h, "") for h in headers],
        value_input_option="USER_ENTERED",
    )

# ------------------------- Coordinated UPSERT -------------------------

def upsert_nannies(
    sa_json: str,
    spreadsheet_id: str,
    scraped_rows: List[dict],
    *,
    new_only: bool = False,
) -> Tuple[int, int]:
    """
    Upsert all scraped rows into the Nannies sheet.
    Returns (new_inserted, updated_existing).
    """
    ws, header_map, id_to_row = load_existing_ids(sa_json, spreadsheet_id)

    to_insert: List[dict] = []
    to_update_by_row: Dict[int, dict] = {}

    now_iso = dt.datetime.now().astimezone().isoformat(timespec="seconds")

    for r in scraped_rows:
        pid = str(r.get("profile_id") or "").strip()
        if not pid:
            # cannot index without ID; skip
            continue

        # Ensure required fields exist
        r.setdefault("profile_url", "")
        r.setdefault("first_seen_at", now_iso)
        r.setdefault("last_seen_at", now_iso)
        # default human fields for NEW rows
        r.setdefault("status", "New")
        r.setdefault("owner", "")
        r.setdefault("notes", "")
        r.setdefault("last_contacted_at", "")

        if pid not in id_to_row:
            # NEW
            to_insert.append(r)
        else:
            if new_only:
                # Don't touch existing rows
                continue
            row_idx = id_to_row[pid]
            # Prepare partial update payload (machine-only)
            upd = {k: r.get(k) for k in set(MACHINE_UPDATE_COLS)}
            # Always refresh last_seen_at
            upd["last_seen_at"] = now_iso
            to_update_by_row[row_idx] = upd

    new_count = append_new_rows(ws, header_map, to_insert)
    upd_count = 0 if new_only else batch_update_machine_fields(ws, header_map, to_update_by_row)
    return new_count, upd_count
