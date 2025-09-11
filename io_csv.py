# io_csv.py
from pathlib import Path
import csv
from typing import Dict

def append_row(row: Dict, path: Path):
    """
    Append a single dict row to CSV, creating file with headers if needed.
    Headers are taken from the row's keys (keep them stable across writes).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = path.exists()

    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)
