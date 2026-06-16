"""Google Sheets backend for PPT Job App (cloud + multi-user)."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Callable, TypeVar

import gspread
import pandas as pd
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError

T = TypeVar("T")

_sp_cache = None
_worksheets_verified = False

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_HEADERS = {
    "Clients": ["client", "phone", "email", "address", "date_added"],
    "Jobs": [
        "job_no",
        "job_name",
        "client",
        "area_manager",
        "team_leader",
        "start_date",
        "total_labour",
        "man_days_available",
        "status",
        "date_created",
    ],
    "Quote_Areas": [
        "id",
        "job_no",
        "quote_area",
        "unit",
        "quantity",
        "description",
        "prod_qty_per_md",
    ],
    "Attendance": ["id", "job_no", "painter_name", "emp_id", "hourly_rate"]
    + [f"day{i}" for i in range(1, 15)],
    "Bonus_Log": [
        "job_no",
        "man_days_available",
        "actual_man_days",
        "days_saved",
        "bonus_rate",
        "total_bonus_pool",
        "bonus_per_painter",
    ],
    "Custom_Rates": [
        "sort_order",
        "item",
        "unit",
        "material",
        "labour",
        "default_job_notes",
        "date_updated",
    ],
    "Additional_Rates": ["sort_order", "item", "rate_unit", "rate_value", "date_updated"],
}


def _app_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _creds_path() -> str:
    return os.path.join(_app_dir(), "credentials.json")


def _config_path() -> str:
    return os.path.join(_app_dir(), "sheet_config.json")


def _normalize_service_account_info(info: dict) -> dict:
    normalized = dict(info)
    private_key = normalized.get("private_key", "")
    if isinstance(private_key, str) and "\\n" in private_key:
        normalized["private_key"] = private_key.replace("\\n", "\n")
    return normalized


def _load_credentials() -> Credentials | None:
    try:
        import streamlit as st

        for key in ("gcp_service_account", "gcp"):
            if key in st.secrets:
                return Credentials.from_service_account_info(
                    _normalize_service_account_info(dict(st.secrets[key])),
                    scopes=SCOPES,
                )
    except Exception:
        pass

    path = _creds_path()
    if os.path.isfile(path):
        return Credentials.from_service_account_file(path, scopes=SCOPES)
    return None


def _verify_credentials(creds: Credentials) -> None:
    """Refresh the token early so auth problems surface with a clear message."""
    try:
        creds.refresh(Request())
    except RefreshError as exc:
        email = getattr(creds, "service_account_email", "unknown")
        raise RuntimeError(
            f"Google authentication failed for service account {email}. "
            "Google reported that this account does not exist or its key was revoked. "
            "In Google Cloud Console → IAM & Admin → Service Accounts, confirm the account "
            "still exists, create a new JSON key if needed, then update credentials.json "
            "and Streamlit secrets with the new file."
        ) from exc


def _get_spreadsheet_id() -> str | None:
    try:
        import streamlit as st

        if "spreadsheet_id" in st.secrets:
            return str(st.secrets["spreadsheet_id"]).strip() or None
    except Exception:
        pass

    config_path = _config_path()
    if os.path.isfile(config_path):
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
            return (data.get("spreadsheet_id") or "").strip() or None
    return None


def is_configured() -> bool:
    return bool(_load_credentials() and _get_spreadsheet_id())


def get_service_account_email() -> str:
    creds = _load_credentials()
    if creds is None:
        raise RuntimeError("credentials.json not found and no Streamlit secrets configured")
    return creds.service_account_email


def save_spreadsheet_id(sheet_id: str) -> None:
    with open(_config_path(), "w", encoding="utf-8") as f:
        json.dump({"spreadsheet_id": sheet_id.strip()}, f, indent=2)


def _reset_connection_cache() -> None:
    global _sp_cache, _worksheets_verified
    _sp_cache = None
    _worksheets_verified = False


def _retry_on_quota(func: Callable[..., T], *args, **kwargs) -> T:
    """Retry Google Sheets calls when the per-minute read/write quota is hit."""
    for attempt in range(5):
        try:
            return func(*args, **kwargs)
        except APIError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 429 and attempt < 4:
                time.sleep(min(2**attempt * 3, 45))
                continue
            raise


def connect():
    global _sp_cache, _worksheets_verified

    if _sp_cache is not None:
        return _sp_cache

    creds = _load_credentials()
    if creds is None:
        raise RuntimeError(
            "Google credentials not found. Add credentials.json or Streamlit secrets."
        )
    _verify_credentials(creds)
    sheet_id = _get_spreadsheet_id()
    if not sheet_id:
        raise RuntimeError(
            "Spreadsheet ID not configured. Run setup_sheets.py or set spreadsheet_id in secrets."
        )
    gc = gspread.authorize(creds)
    sp = _retry_on_quota(gc.open_by_key, sheet_id)
    _retry_on_quota(_ensure_worksheets, sp)
    _worksheets_verified = True
    _sp_cache = sp
    return sp


def _ensure_worksheets(sp) -> None:
    existing = {ws.title for ws in sp.worksheets()}
    for name, headers in SHEET_HEADERS.items():
        if name not in existing:
            ws = sp.add_worksheet(title=name, rows=1000, cols=max(len(headers), 1))
            _retry_on_quota(ws.update, [headers], "A1")


def _worksheet(name: str):
    return connect().worksheet(name)


def _read_dataframe(name: str) -> pd.DataFrame:
    if not is_configured():
        return pd.DataFrame(columns=SHEET_HEADERS[name])
    ws = _worksheet(name)
    records = _retry_on_quota(ws.get_all_records)
    headers = SHEET_HEADERS[name]
    if not records:
        return pd.DataFrame(columns=headers)
    return pd.DataFrame(records)


def _upsert_by_key(name: str, key_field: str, key_value: str, row: dict) -> None:
    ws = _worksheet(name)
    headers = SHEET_HEADERS[name]
    values = _retry_on_quota(ws.get_all_values)
    if not values:
        _retry_on_quota(ws.update, [headers], "A1")
        values = [headers]

    header_row = values[0]
    try:
        key_idx = header_row.index(key_field)
    except ValueError:
        key_idx = headers.index(key_field)

    row_values = [str(row.get(h, "") or "") for h in headers]
    key_value = str(key_value)

    for row_num, existing in enumerate(values[1:], start=2):
        if len(existing) > key_idx and str(existing[key_idx]) == key_value:
            _retry_on_quota(ws.update, [row_values], f"A{row_num}")
            return

    _retry_on_quota(ws.append_row, row_values, value_input_option="USER_ENTERED")


def _replace_sheet(name: str, rows: list[dict]) -> None:
    ws = _worksheet(name)
    headers = SHEET_HEADERS[name]
    data = [headers]
    for row in rows:
        data.append([row.get(h, "") for h in headers])
    _retry_on_quota(ws.clear)
    _retry_on_quota(ws.update, data, "A1")


# ====================== CLIENTS ======================
def save_client(client, phone="", email="", address=""):
    if not is_configured():
        raise RuntimeError("Google Sheets is not configured")
    if not str(client or "").strip():
        raise ValueError("Client name is required")
    _upsert_by_key(
        "Clients",
        "client",
        str(client).strip(),
        {
            "client": str(client).strip(),
            "phone": phone or "",
            "email": email or "",
            "address": address or "",
            "date_added": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    )


def get_all_clients():
    return _read_dataframe("Clients")


# ====================== JOBS ======================
def save_job(
    job_no,
    job_name,
    client,
    area_manager,
    team_leader,
    start_date,
    total_labour,
    man_days_available,
):
    if not is_configured():
        raise RuntimeError("Google Sheets is not configured")
    _upsert_by_key(
        "Jobs",
        "job_no",
        str(job_no),
        {
            "job_no": str(job_no),
            "job_name": job_name or "",
            "client": client or "",
            "area_manager": area_manager or "",
            "team_leader": team_leader or "",
            "start_date": str(start_date),
            "total_labour": float(total_labour or 0),
            "man_days_available": float(man_days_available or 0),
            "status": "Open",
            "date_created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    )


def get_all_jobs():
    df = _read_dataframe("Jobs")
    if not df.empty and "job_no" in df.columns:
        df = df.copy()
        df["Job No"] = df["job_no"].astype(str)
    elif df.empty:
        df = pd.DataFrame(columns=SHEET_HEADERS["Jobs"] + ["Job No"])
    return df


def get_quote_areas(job_no=None):
    df = _read_dataframe("Quote_Areas")
    if job_no and not df.empty and "job_no" in df.columns:
        df = df[df["job_no"].astype(str) == str(job_no)]
    return df


def get_attendance(job_no=None):
    df = _read_dataframe("Attendance")
    if job_no and not df.empty and "job_no" in df.columns:
        df = df[df["job_no"].astype(str) == str(job_no)]
    return df


def get_bonus_log():
    return _read_dataframe("Bonus_Log")


def get_job_history():
    jobs = get_all_jobs()
    clients = get_all_clients()
    if jobs.empty:
        return jobs
    if clients.empty or "client" not in clients.columns:
        return jobs
    return jobs.merge(clients, on="client", how="left", suffixes=("", "_client"))


# ====================== CUSTOM RATES ======================
def save_custom_rates(item_rates_dict, item_units_dict, default_job_notes_dict):
    if not is_configured():
        raise RuntimeError("Google Sheets is not configured")
    date_updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for sort_order, (item, rates) in enumerate(item_rates_dict.items(), start=1):
        rows.append(
            {
                "sort_order": sort_order,
                "item": item,
                "unit": item_units_dict.get(item, "m²"),
                "material": rates.get("material", 0),
                "labour": rates.get("labour", 0),
                "default_job_notes": default_job_notes_dict.get(item, ""),
                "date_updated": date_updated,
            }
        )
    _replace_sheet("Custom_Rates", rows)


def load_custom_rates():
    if not is_configured():
        return {}, {}, {}, {}
    df = _read_dataframe("Custom_Rates")
    if df.empty:
        return {}, {}, {}, {}

    if "sort_order" in df.columns:
        df = df.sort_values(["sort_order", "item"], na_position="last")
    else:
        df = df.sort_values("item")

    item_rates = {}
    item_units = {}
    default_notes = {}
    sort_orders = {}
    for _, row in df.iterrows():
        item = str(row.get("item", "") or "").strip()
        if not item:
            continue
        item_rates[item] = {
            "material": float(row.get("material", 0) or 0),
            "labour": float(row.get("labour", 0) or 0),
        }
        item_units[item] = str(row.get("unit", "m²") or "m²")
        default_notes[item] = str(row.get("default_job_notes", "") or "")
        sort_orders[item] = int(row.get("sort_order") or 0)
    return item_rates, item_units, default_notes, sort_orders


# ====================== ADDITIONAL RATES ======================
def save_custom_additional_rates(rows):
    if not is_configured():
        raise RuntimeError("Google Sheets is not configured")
    date_updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = []
    for row in rows:
        payload.append(
            {
                "sort_order": int(row["sort_order"]),
                "item": row["item"],
                "rate_unit": row["rate_unit"],
                "rate_value": float(row["rate_value"]),
                "date_updated": date_updated,
            }
        )
    _replace_sheet("Additional_Rates", payload)


def load_custom_additional_rates():
    if not is_configured():
        return []
    df = _read_dataframe("Additional_Rates")
    if df.empty:
        return []
    if "sort_order" in df.columns:
        df = df.sort_values(["sort_order", "item", "rate_unit"])
    return [
        {
            "sort_order": int(row["sort_order"]),
            "item": row["item"],
            "rate_unit": row["rate_unit"],
            "rate_value": float(row["rate_value"]),
        }
        for _, row in df.iterrows()
    ]
