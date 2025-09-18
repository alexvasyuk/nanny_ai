# gsheets.py
from __future__ import annotations

import datetime as dt
from typing import Dict, List, Tuple, Optional, Any
import re
import gspread
from gspread.cell import Cell
from gspread_formatting import cellFormat, color, format_cell_range
from gspread_formatting import DataValidationRule, BooleanCondition, set_data_validation_for_cell_range
from gspread.utils import rowcol_to_a1

# ------------------------- Sheet & headers -------------------------

NANNIES_SHEET = "Nannies"
RUNS_SHEET    = "Runs"

# Columns we write on insert
MACHINE_NANNIES_HEADERS: List[str] = [
    "profile_id",
    "profile_url",
    "name",
    "phone",       
    "location",
    "travel_time_min", 
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
]

HUMAN_NANNIES_HEADERS: List[str] = [
    "status",
    "notes",
    "last_contacted_at",
]

NANNIES_HEADERS: List[str] = MACHINE_NANNIES_HEADERS + HUMAN_NANNIES_HEADERS

# For updates on existing rows, we ONLY touch these:
MACHINE_UPDATE_COLS = [
    "last_active_raw",
    "last_active_at",
    "last_seen_at",
    "profile_url",  
    # optionally keep score fresh—include the two below if you want that
    # "score",
    # "explanation_bullets",
]

PID_FIELD = "profile_id"
URL_FIELD = "profile_url"

# Full set of allowed statuses (canonical, ordered)
STATUSES_ALLOWED = [
    "Новый",
    "Не подходит",                         # <— added
    "Дубликат",
    "Не соответствует критериям",
    "Неверный номер",
    "Не отвечает",
    "Оставлено сообщение",
    "Не заинтересован(а)",
    "Заинтересован(а) / ждёт скрининг",
    "Скрининг назначен",
    "Скрининг пройден ✅ успешно",
    "Скрининг пройден ❌ отказ",
    "Передано Саше/Вике",
    "У Саши/Вики интервью назначено",
    "У Саши/Вики интервью проведено ✅ успешно",
    "У Саши/Вики интервью проведено ❌ отказ",
    "Сделано предложение",
    "Нанят(а)",
    "В ожидании",
]

# === One-time sheet context cache (avoid re-opening/re-reading per page) ===
_SHEETS_CTX: Dict[tuple[str, str], Dict[str, Any]] = {}

def get_or_init_ctx(sa_json: str, spreadsheet_id: str) -> Dict[str, Any]:
    key = (sa_json, spreadsheet_id)
    ctx = _SHEETS_CTX.get(key)
    if ctx:
        return ctx
    ws, header_map, id_to_row, existing_urls_by_id = load_existing_ids(sa_json, spreadsheet_id)
    ctx = {
        "ws": ws,
        "header_map": header_map,
        "id_to_row": id_to_row,
        "existing_urls_by_id": existing_urls_by_id,
    }
    _SHEETS_CTX[key] = ctx
    return ctx

def canon_pid(x) -> str:
    """
    Return a canonical profile_id as a *string*.
    - trims spaces
    - keeps digits only (Nashanyanya ids are numeric)
    """
    s = str(x).strip()
    s = re.sub(r"\D+", "", s)
    return s

def canon_url(u: str) -> str:
    """Normalize profile_url so we don't treat trailing slashes/params as different."""
    if not u:
        return ""
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(u)
    path = p.path.rstrip("/")  # drop trailing slash
    return urlunsplit((p.scheme, p.netloc, path, "", ""))  # strip query/frag

def _col_letter(col_idx: int) -> str:
    # 1 -> "A", 27 -> "AA"
    return "".join(ch for ch in rowcol_to_a1(1, col_idx) if ch.isalpha())

def sort_by_header(ws, header_name: str, desc: bool = True, header_row: int = 1) -> bool:
    """
    Sort rows (below the header) by a column identified by its header text.
    Returns True if sorted, False if header not found.

    ws: gspread worksheet
    header_name: e.g. "score"
    desc: True for Z→A (highest first)
    header_row: row index of the header (1-based)
    """
    header = ws.row_values(header_row)
    if header_name not in header:
        return False

    col_idx = header.index(header_name) + 1
    last_col_letter = _col_letter(len(header))
    rng = f"A{header_row + 1}:{last_col_letter}{ws.row_count}"  # keep header out of the sort
    order = "des" if desc else "asc"
    ws.sort((col_idx, order), range=rng)
    return True


def bold_columns_by_headers(ws, header_names: list[str] = None):
    header = ws.row_values(1)
    if not header_names:
        return  # nothing to do
    for name in header_names:
        if name in header:
            col_idx = header.index(name) + 1
            col_letter = "".join(ch for ch in rowcol_to_a1(1, col_idx) if ch.isalpha())
            ws.format(f"{col_letter}:{col_letter}", {"textFormat": {"bold": True}})

def ensure_status_dropdown(ws):
    try:
        sheet_id = getattr(ws, "id", None) or ws._properties["sheetId"]
        grid = ws._properties.get("gridProperties", {})
        row_count = grid.get("rowCount", 5000)  # fallback if not present

        # Which column to target
        col_idx0 = NANNIES_HEADERS.index("status")  # zero-based
        req = {
            "setDataValidation": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,           # skip header row
                    "endRowIndex": row_count,
                    "startColumnIndex": col_idx0,
                    "endColumnIndex": col_idx0 + 1,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": s} for s in STATUSES_ALLOWED],
                    },
                    "inputMessage": "Select a status",
                    "strict": True,       # disallow values outside the list
                    "showCustomUi": True, # show dropdown arrow
                },
            }
        }

        ws.spreadsheet.batch_update({"requests": [req]})
    except Exception as e:
        print(f"[SHEETS] Could not set status dropdown: {e}")


def hide_columns(ws, headers_to_hide):
    """
    Hide columns by header name using gspread's batch_update (no googleapiclient).
    """
    try:
        sheet_id = getattr(ws, "id", None) or ws._properties["sheetId"]

        requests = []
        for header in headers_to_hide:
            if header in NANNIES_HEADERS:
                col_idx = NANNIES_HEADERS.index(header)  # zero-based
                requests.append({
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": col_idx,
                            "endIndex": col_idx + 1,
                        },
                        "properties": {"hiddenByUser": True},
                        "fields": "hiddenByUser",
                    }
                })

        if requests:
            ws.spreadsheet.batch_update({"requests": requests})
    except Exception as e:
        print(f"[SHEETS] Could not hide columns: {e}")

def apply_column_colors(sheet):
    """
    Color-code columns so that machine-populated ones are gray
    and user-populated ones stay white.
    """
    # Define which columns are machine vs user
    machine_set =  set(MACHINE_NANNIES_HEADERS)
    user_set = set(HUMAN_NANNIES_HEADERS)

    # Read header row
    header = sheet.row_values(1)

    # Find column indexes (1-based for Google Sheets API)
    machine_indexes = [i+1 for i, h in enumerate(header) if h in machine_set]
    user_indexes = [i+1 for i, h in enumerate(header) if h in user_set]

    # Define colors
    gray_format = cellFormat(backgroundColor=color(0.9, 0.9, 0.9))  # light gray
    white_format = cellFormat(backgroundColor=color(1, 1, 1))       # white

    # Apply colors (rows 2–1000, change range if needed)
    for col in machine_indexes:
        col_letter = chr(64+col)  # 1->A, 2->B...
        format_cell_range(sheet, f"{col_letter}2:{col_letter}1000", gray_format)

    for col in user_indexes:
        col_letter = chr(64+col)
        format_cell_range(sheet, f"{col_letter}2:{col_letter}1000", white_format)


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

def load_existing_ids(sa_json: str, spreadsheet_id: str):
    """
    Return (worksheet, header_map, id_to_row, existing_urls_by_id).
    id_to_row: profile_id -> row index (2-based)
    existing_urls_by_id: profile_id -> profile_url (if present)
    """
    sh = _open_sheet(sa_json, spreadsheet_id)
    ws = _get_or_create_ws(sh, NANNIES_SHEET)
    header_map = _ensure_headers(ws, NANNIES_HEADERS)

    #Formatting the sheet: hide some columns, ensure dropdown
    hide_columns(ws, ["last_active_raw", "last_active_at", "first_seen_at", "last_seen_at"])
    # after you ensure headers / open ws
    ensure_status_dropdown(ws)
    bold_columns_by_headers(ws, ["score"])  # adjust to your exact header text

    # === FAST COLUMN READS: 1–2 API calls instead of per-row ===
    id_to_row: Dict[str, int] = {}
    existing_urls_by_id: Dict[str, str] = {}

    pid_col = header_map.get("profile_id")
    url_col = header_map.get("profile_url")

    # Nothing to map if we don't have a profile_id column
    if pid_col is None:
        return ws, header_map, id_to_row, existing_urls_by_id

    # Read entire columns once (each is a single API read). First element is header.
    pid_values: List[str] = ws.col_values(pid_col) if pid_col else []
    url_values: List[str] = ws.col_values(url_col) if url_col else []

    # Drop header row (row 1)
    if pid_values:
        pid_values = pid_values[1:]
    if url_values:
        url_values = url_values[1:]

    # Build maps. Spreadsheet row index starts at 2 here.
    for i, pid_cell in enumerate(pid_values, start=2):
        if not pid_cell:
            continue
        pid = canon_pid(pid_cell)
        if not pid:
            continue

        id_to_row[p_id] = i if (p_id := pid) else i  # keep exact row index

        # Attach canonical URL if available at same offset
        if url_values:
            j = i - 2
            if 0 <= j < len(url_values):
                u = url_values[j] or ""
                if u:
                    existing_urls_by_id[p_id] = canon_url(u)

    return ws, header_map, id_to_row, existing_urls_by_id


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
    ctx: Optional[dict] = None,   # <— NEW
):
    if ctx is None:
        ws, header_map, id_to_row, existing_urls_by_id = load_existing_ids(sa_json, spreadsheet_id)
    else:
        ws = ctx["ws"]
        header_map = ctx["header_map"]
        id_to_row = ctx["id_to_row"]
        existing_urls_by_id = ctx["existing_urls_by_id"]

    to_insert: List[dict] = []
    to_update_by_row: Dict[int, dict] = {}
    now_iso = dt.datetime.now().astimezone().isoformat(timespec="seconds")

    for r in scraped_rows:
        # === INSIDE for r in scraped_rows: canonicalize first ===
        pid_raw = r.get(PID_FIELD) or r.get("id") or r.get("profileId")
        pid = canon_pid(pid_raw or "")
        if not pid:
            continue  # skip rows without a valid id

        # normalize url -> profile_url
        if r.get(URL_FIELD):
            r[URL_FIELD] = canon_url(r[URL_FIELD])
        elif r.get("url"):
            r[URL_FIELD] = canon_url(r["url"])

        # ensure we always store pid as *string*
        r[PID_FIELD] = pid

        # normalize url -> profile_url
        r.setdefault("profile_url", r.get("url", ""))

        # defaults for NEW rows
        r.setdefault("first_seen_at", now_iso)
        r.setdefault("last_seen_at", now_iso)
        r.setdefault("status", "Новый")
        r.setdefault("notes", "")
        r.setdefault("last_contacted_at", "")

        if pid not in id_to_row:
            to_insert.append(r)
            continue

        if new_only:
            continue

        # === INSERT/UPDATE decision strictly by pid ===
        row_idx = id_to_row.get(pid)
        if row_idx is None:
            # build minimal new row (respect your header_map)
            to_insert.append(r)
        else:
            upd: Dict[str, Optional[str]] = {}

            # Update machine fields you expect to change
            for k in MACHINE_UPDATE_COLS:
                if k in r:
                    upd[k] = r[k]

            # backfill/normalize profile_url only if blank in sheet or changed after canon
            if r.get(URL_FIELD):
                sheet_url = existing_urls_by_id.get(pid, "")
                if not sheet_url or sheet_url != r[URL_FIELD]:
                    upd[URL_FIELD] = r[URL_FIELD]

            to_update_by_row[row_idx] = upd

    new_count = append_new_rows(ws, header_map, to_insert)
    upd_count = 0 if new_only else batch_update_machine_fields(ws, header_map, to_update_by_row)
    apply_column_colors(ws)
    # sort by score descending
    sort_by_header(ws, "score", desc=True)

    # Keep in-memory maps in sync for this process (prevents re-adding within the run)
    if ctx is not None:
        for r in scraped_rows or []:
            pid = canon_pid(r.get("profile_id") or "")
            if not pid:
                continue
            if pid not in id_to_row:
                id_to_row[pid] = id_to_row.get(pid, -1)
            u = r.get("profile_url")
            if u:
                existing_urls_by_id[pid] = u

    return new_count, upd_count

