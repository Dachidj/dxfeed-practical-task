"""Upload output/holidays.csv to Google Drive (existing CSV file) and Google Sheets (existing sheet).

Both targets must already exist and be shared with the service account (Editor): the service
account has no storage quota, so this only ever updates files, never creates them.
Config comes from .env (see .env.example): GOOGLE_CREDENTIALS_FILE, SHEET_ID, CSV_FILE_ID.
"""
from __future__ import annotations

import csv
import logging
import os
from pathlib import Path

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

log = logging.getLogger("upload")

PROJECT_ROOT = Path(__file__).resolve().parent
SCOPES = ["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets"]


def _config() -> tuple[Path, str, str] | None:
    """(credentials file, sheet id, csv file id), or None if .env / the key file is missing."""
    load_dotenv(PROJECT_ROOT / ".env")
    names = ("GOOGLE_CREDENTIALS_FILE", "SHEET_ID", "CSV_FILE_ID")
    if not all(os.getenv(n) for n in names):
        return None
    creds_file = PROJECT_ROOT / os.environ["GOOGLE_CREDENTIALS_FILE"]
    if not creds_file.is_file():
        return None
    return creds_file, os.environ["SHEET_ID"], os.environ["CSV_FILE_ID"]


def update_drive_csv(creds: Credentials, file_id: str, csv_path: Path) -> None:
    """Replace the content of the existing Drive file (files.update with new media)."""
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    media = MediaFileUpload(str(csv_path), mimetype="text/csv", resumable=False)
    f = drive.files().update(fileId=file_id, media_body=media, supportsAllDrives=True,
                             fields="name,size,modifiedTime").execute()
    log.info("Drive CSV updated: %s (%s bytes, modified %s)", f["name"], f.get("size"), f["modifiedTime"])


def update_sheet(creds: Credentials, sheet_id: str, rows: list[list[str]]) -> bool:
    """Rewrite the first tab with header + rows; read back and compare the row count.
    New rows are written first and only the leftover rows below are cleared afterwards,
    so a failure part-way can never leave an empty Sheet."""
    ws = gspread.authorize(creds).open_by_key(sheet_id).get_worksheet(0)
    n_rows, n_cols = len(rows), len(rows[0])
    if ws.row_count < n_rows or ws.col_count < n_cols:  # make room, never shrink yet
        ws.resize(rows=max(ws.row_count, n_rows), cols=max(ws.col_count, n_cols))
    ws.update(range_name="A1", values=rows, value_input_option="RAW")  # RAW: dates stay text
    if ws.row_count > n_rows:  # old table was longer: clear what is left below the new rows
        ws.batch_clear([f"{n_rows + 1}:{ws.row_count}"])
    ws.resize(rows=n_rows, cols=n_cols)
    ws.freeze(rows=1)
    ws.format("1:1", {"textFormat": {"bold": True}})
    log.info("Sheet updated: tab %r, %d data rows + header", ws.title, len(rows) - 1)

    got = len(ws.get_all_values())
    if got == len(rows):
        log.info("Sheet read-back OK: %d rows incl. header match the CSV", got)
        return True
    log.warning("Sheet read-back mismatch: sheet has %d rows, CSV has %d (incl. header)", got, len(rows))
    return False


def upload(csv_path: Path) -> bool:
    """Upload to Drive + Sheets. Returns False if the read-back check failed; raises on API errors.
    Not configured (no .env or key file) -> logs and returns True, so the run still exits 0."""
    config = _config()
    if config is None:
        log.warning("Google upload not configured — skipped (see README)")
        return True
    creds_file, sheet_id, csv_file_id = config
    creds = Credentials.from_service_account_file(str(creds_file), scopes=SCOPES)
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    update_drive_csv(creds, csv_file_id, csv_path)
    return update_sheet(creds, sheet_id, rows)
