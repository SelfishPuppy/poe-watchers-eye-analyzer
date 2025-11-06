"""
Google Sheets synchronization helpers for the Watcher's Eye analyzer.

This module handles OAuth authentication and writes CSV exports into
named worksheets so that a Google Sheet can stay up to date with the
latest batch results.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import List, Sequence

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

CREDENTIALS_PATH = Path("config/google_credentials.json")
TOKEN_PATH = Path("config/google_token.json")
SETTINGS_PATH = Path("config/google_sheets_config.json")


class GoogleSheetsConfigError(RuntimeError):
    """Raised when the Sheets configuration file is missing or malformed."""


def load_settings(path: Path = SETTINGS_PATH) -> dict:
    if not path.exists():
        raise GoogleSheetsConfigError(
            f"Configuration file not found at {path}. "
            "Create it with keys: spreadsheet_id, single_sheet_name, double_sheet_name."
        )
    try:
        with path.open("r", encoding="utf-8") as cfg_file:
            data = json.load(cfg_file)
    except json.JSONDecodeError as exc:
        raise GoogleSheetsConfigError(f"Invalid JSON in {path}: {exc}") from exc

    required_keys = {"spreadsheet_id", "single_sheet_name", "double_sheet_name"}
    missing = required_keys - data.keys()
    if missing:
        raise GoogleSheetsConfigError(
            f"Missing keys in {path}: {', '.join(sorted(missing))}"
        )
    return data


def get_credentials(
    credentials_path: Path = CREDENTIALS_PATH,
    token_path: Path = TOKEN_PATH,
) -> Credentials:
    if not credentials_path.exists():
        raise GoogleSheetsConfigError(
            f"OAuth client secrets not found at {credentials_path}."
        )

    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_path, SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        with token_path.open("w", encoding="utf-8") as token_file:
            token_file.write(creds.to_json())

    return creds


def read_csv_values(csv_path: Path) -> List[List[str]]:
    with csv_path.open("r", encoding="utf-8", newline="") as csv_file:
        reader = csv.reader(csv_file)
        return [row for row in reader]


def upload_csv_to_google_sheet(csv_path: Path, mode: str) -> None:
    settings = load_settings()
    creds = get_credentials()

    sheet_name_key = "single_sheet_name" if mode == "single" else "double_sheet_name"
    sheet_name = settings[sheet_name_key]
    spreadsheet_id = settings["spreadsheet_id"]
    values = read_csv_values(Path(csv_path))

    service = build("sheets", "v4", credentials=creds)
    sheets_api = service.spreadsheets()

    range_name = f"{sheet_name}!A1"
    sheets_api.values().clear(
        spreadsheetId=spreadsheet_id, range=range_name
    ).execute()
    body = {"values": values}
    sheets_api.values().update(
        spreadsheetId=spreadsheet_id,
        range=range_name,
        valueInputOption="RAW",
        body=body,
    ).execute()
