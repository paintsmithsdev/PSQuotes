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
try:
    from gspread.exceptions import CellNotFound  # removed in gspread ≥ 6; find() returns None instead
except ImportError:
    class CellNotFound(Exception):  # type: ignore[no-redef]
        pass

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Custom exception hierarchy
# ---------------------------------------------------------------------------

class SheetsUnavailableError(RuntimeError):
    """Google Sheets API is temporarily unreachable (network or service outage)."""


class SheetsAuthError(RuntimeError):
    """Google service account authentication failed."""


class SheetsConfigError(RuntimeError):
    """Google Sheets credentials or spreadsheet ID are not configured."""


# Import requests exceptions for network-level error detection.
# gspread uses requests internally; these can bubble up unwrapped.
try:
    from requests.exceptions import ConnectionError as _ReqConnectionError
    from requests.exceptions import Timeout as _ReqTimeout
    _NETWORK_ERRORS = (_ReqConnectionError, _ReqTimeout)
except ImportError:  # pragma: no cover
    _NETWORK_ERRORS = (OSError,)

_sp_cache = None
_worksheets_verified = False
_is_configured_cache: bool | None = None

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
    "Paint_Lines": [
        "job_no", "section_idx", "paint_class", "type",
        "area_description", "item_idx", "item", "method", "area_m2", "job_notes",
    ],
    "Additional_Lines": [
        "job_no", "sort_order", "item", "duration_days", "km", "liters",
    ],
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
        raise SheetsAuthError(
            f"Google authentication failed for service account '{email}'. "
            "The account may not exist or its key was revoked. "
            "In Google Cloud Console → IAM & Admin → Service Accounts, confirm the account "
            "still exists, create a new JSON key if needed, then update credentials.json "
            "and Streamlit secrets with the new file."
        ) from exc
    except _NETWORK_ERRORS as exc:
        raise SheetsUnavailableError(
            "Could not reach Google's authentication servers. "
            "Check your internet connection."
        ) from exc
    except (OSError, TimeoutError) as exc:
        raise SheetsUnavailableError(
            "Network error while verifying Google credentials."
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
    global _is_configured_cache
    if _is_configured_cache is None:
        _is_configured_cache = bool(_load_credentials() and _get_spreadsheet_id())
    return _is_configured_cache


def get_service_account_email() -> str:
    creds = _load_credentials()
    if creds is None:
        raise RuntimeError("credentials.json not found and no Streamlit secrets configured")
    return creds.service_account_email


def save_spreadsheet_id(sheet_id: str) -> None:
    with open(_config_path(), "w", encoding="utf-8") as f:
        json.dump({"spreadsheet_id": sheet_id.strip()}, f, indent=2)


def _reset_connection_cache() -> None:
    global _sp_cache, _worksheets_verified, _is_configured_cache
    _sp_cache = None
    _worksheets_verified = False
    _is_configured_cache = None


def _retry_on_quota(func: Callable[..., T], *args, **kwargs) -> T:
    """Retry on quota (429), server errors (5xx), and transient network failures."""
    last_exc: Exception | None = None
    for attempt in range(5):
        try:
            return func(*args, **kwargs)
        except APIError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            last_exc = exc
            if status == 429 and attempt < 4:
                # Quota exceeded — exponential back-off, up to 45 s
                time.sleep(min(2**attempt * 3, 45))
                continue
            if status is not None and status >= 500 and attempt < 4:
                # Server-side error — retry with shorter back-off
                time.sleep(min(2**attempt * 2, 30))
                continue
            if status is not None and status >= 500:
                raise SheetsUnavailableError(
                    f"Google Sheets returned HTTP {status}. "
                    "The service appears to be temporarily unavailable — "
                    "please try again in a few minutes."
                ) from exc
            raise
        except _NETWORK_ERRORS as exc:
            last_exc = exc
            if attempt < 4:
                time.sleep(min(2**attempt * 2, 30))
                continue
            raise SheetsUnavailableError(
                "Could not reach Google Sheets — network or service unavailable. "
                "Check your internet connection and try again."
            ) from exc
        except (OSError, TimeoutError) as exc:
            last_exc = exc
            if attempt < 4:
                time.sleep(min(2**attempt * 2, 30))
                continue
            raise SheetsUnavailableError(
                "Lost connection to Google Sheets. "
                "Check your internet connection and try again."
            ) from exc
    raise SheetsUnavailableError(
        "Google Sheets is unavailable after repeated attempts. Please try again later."
    ) from last_exc


def connect():
    global _sp_cache, _worksheets_verified

    if _sp_cache is not None:
        return _sp_cache

    creds = _load_credentials()
    if creds is None:
        raise SheetsConfigError(
            "Google credentials not found. Add credentials.json or Streamlit secrets."
        )
    _verify_credentials(creds)
    sheet_id = _get_spreadsheet_id()
    if not sheet_id:
        raise SheetsConfigError(
            "Spreadsheet ID not configured. Run setup_sheets.py or set spreadsheet_id in secrets."
        )
    try:
        gc = gspread.authorize(creds)
        sp = _retry_on_quota(gc.open_by_key, sheet_id)
    except (SheetsUnavailableError, SheetsAuthError, SheetsConfigError):
        _reset_connection_cache()
        raise
    except APIError as exc:
        _reset_connection_cache()
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 404:
            raise SheetsConfigError(
                f"Spreadsheet not found (ID: {sheet_id}). "
                "Check the spreadsheet ID and ensure the service account has been granted access."
            ) from exc
        raise SheetsUnavailableError(
            f"Google Sheets returned HTTP {status} while opening the spreadsheet. "
            "The service may be temporarily unavailable."
        ) from exc
    except Exception as exc:
        _reset_connection_cache()
        raise SheetsUnavailableError(
            f"Could not connect to Google Sheets: {exc}"
        ) from exc
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
    row_values = [str(row.get(h, "") or "") for h in headers]
    key_value_str = str(key_value).strip()

    try:
        key_col = headers.index(key_field) + 1  # gspread uses 1-based columns
    except ValueError:
        key_col = 1

    # Ensure the header row exists (cheap single-cell check)
    try:
        if not _retry_on_quota(ws.acell, "A1").value:
            _retry_on_quota(ws.update, [headers], "A1")
    except Exception:
        _retry_on_quota(ws.update, [headers], "A1")

    # Use find() to locate the existing row — far cheaper than get_all_values()
    try:
        cell = _retry_on_quota(ws.find, key_value_str, in_column=key_col)
        if cell and cell.row > 1:
            _retry_on_quota(ws.update, [row_values], f"A{cell.row}")
            return
    except CellNotFound:
        pass

    _retry_on_quota(ws.append_row, row_values, value_input_option="USER_ENTERED")


def _update_row_fields(name: str, key_field: str, key_value: str, fields: dict) -> bool:
    """Find a row by key and update only the supplied fields in-place.

    Returns True if the row was found and updated, False if not found.
    Unlike _upsert_by_key this never appends a new row.
    """
    ws = _worksheet(name)
    headers = SHEET_HEADERS[name]
    key_value_str = str(key_value).strip()

    try:
        key_col = headers.index(key_field) + 1
    except ValueError:
        return False

    # Use find() to locate the row — avoids fetching the entire sheet
    try:
        cell = _retry_on_quota(ws.find, key_value_str, in_column=key_col)
        if not cell or cell.row <= 1:
            return False
    except CellNotFound:
        return False

    # Fetch only the single target row to read existing values
    end_col_letter = chr(64 + len(headers)) if len(headers) <= 26 else "Z"
    row_range = f"A{cell.row}:{end_col_letter}{cell.row}"
    existing_data = _retry_on_quota(ws.get, row_range)
    existing = list(existing_data[0]) if existing_data else []
    existing_padded = existing + [""] * max(0, len(headers) - len(existing))
    updated = list(existing_padded[:len(headers)])
    for field, value in fields.items():
        if field in headers:
            updated[headers.index(field)] = str(value) if value is not None else ""
    _retry_on_quota(ws.update, [updated], f"A{cell.row}")
    return True


def _replace_sheet(name: str, rows: list[dict]) -> None:
    ws = _worksheet(name)
    headers = SHEET_HEADERS[name]
    data = [headers]
    for row in rows:
        data.append([row.get(h, "") for h in headers])
    _retry_on_quota(ws.clear)
    _retry_on_quota(ws.update, data, "A1")


def _replace_job_rows(name: str, job_no: str, new_rows: list[dict]) -> None:
    """Replace all rows belonging to job_no with new_rows, leaving other jobs intact."""
    ws = _worksheet(name)
    headers = SHEET_HEADERS[name]
    records = _retry_on_quota(ws.get_all_records)
    job_no_str = str(job_no).strip()
    kept = [r for r in records if str(r.get("job_no", "")).strip() != job_no_str]
    all_rows = kept + new_rows
    data = [headers] + [[str(r.get(h, "") or "") for h in headers] for r in all_rows]
    _retry_on_quota(ws.clear)
    _retry_on_quota(ws.update, data, "A1")


def health_check() -> tuple[bool, str]:
    """Test live connectivity to Google Sheets. Returns (ok, message).

    Resets the connection cache on failure so the next real call
    attempts a fresh connection.
    """
    if not is_configured():
        return False, "Google Sheets is not configured."
    try:
        sp = connect()
        _ = sp.title  # lightweight attribute access — no extra API call
        return True, "Connected"
    except SheetsUnavailableError as exc:
        _reset_connection_cache()
        return False, str(exc)
    except SheetsAuthError as exc:
        return False, str(exc)
    except SheetsConfigError as exc:
        return False, str(exc)
    except Exception as exc:
        _reset_connection_cache()
        return False, f"Unexpected error: {exc}"


# ====================== CLIENTS ======================
def save_client(client, phone="", email="", address=""):
    if not is_configured():
        raise SheetsConfigError("Google Sheets is not configured")
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
        raise SheetsConfigError("Google Sheets is not configured")
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


def update_job_fields(job_no: str, fields: dict) -> bool:
    """Update specific fields of an existing job without touching the rest.

    Fields that are NOT in `fields` (e.g. total_labour, date_created) are
    left exactly as they are in the sheet.  Never creates a new row.
    Returns True if the job was found and updated.
    """
    if not is_configured():
        raise SheetsConfigError("Google Sheets is not configured")
    return _update_row_fields("Jobs", "job_no", str(job_no), fields)


def update_client_fields(original_name: str, fields: dict) -> bool:
    """Update specific fields of an existing client record.

    Looks up the client by `original_name` so renaming the client works
    correctly (the old row is updated rather than a new one appended).
    Never creates a new row.  Returns True if the client was found.
    """
    if not is_configured():
        raise SheetsConfigError("Google Sheets is not configured")
    return _update_row_fields("Clients", "client", original_name, fields)


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
        raise SheetsConfigError("Google Sheets is not configured")
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
        raise SheetsConfigError("Google Sheets is not configured")
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


# ====================== QUOTE LINE ITEMS ======================

def save_paint_lines(job_no: str, paint_sections: list) -> None:
    """Persist all paint line items for a quote, replacing any previous data."""
    if not is_configured():
        raise SheetsConfigError("Google Sheets is not configured")
    rows: list[dict] = []
    for si, section in enumerate(paint_sections):
        for ii, item in enumerate(section.get("items", [])):
            rows.append({
                "job_no":            str(job_no),
                "section_idx":       si,
                "paint_class":       str(section.get("paint_class", "A") or "A"),
                "type":              str(section.get("type", "Exterior") or "Exterior"),
                "area_description":  str(section.get("area_description", "") or ""),
                "item_idx":          ii,
                "item":              str(item.get("item", "") or ""),
                "method":            str(item.get("method", "Previously painted") or "Previously painted"),
                "area_m2":           float(item.get("area_m2", 0) or 0),
                "job_notes":         str(item.get("job_notes", "") or ""),
            })
    _replace_job_rows("Paint_Lines", str(job_no), rows)


def save_additional_lines(job_no: str, additional_sections: list) -> None:
    """Persist all additional cost lines for a quote, replacing any previous data."""
    if not is_configured():
        raise SheetsConfigError("Google Sheets is not configured")
    rows: list[dict] = []
    for i, sec in enumerate(additional_sections):
        rows.append({
            "job_no":        str(job_no),
            "sort_order":    i,
            "item":          str(sec.get("item", "") or ""),
            "duration_days": float(sec.get("duration_days", 0) or 0),
            "km":            float(sec.get("km", 0) or 0),
            "liters":        float(sec.get("liters", 0) or 0),
        })
    _replace_job_rows("Additional_Lines", str(job_no), rows)


def get_paint_lines(job_no: str) -> list:
    """Return paint_sections list for the given job, reconstructed from the DB."""
    if not is_configured():
        return []
    df = _read_dataframe("Paint_Lines")
    if df.empty:
        return []
    df = df[df["job_no"].astype(str).str.strip() == str(job_no).strip()]
    if df.empty:
        return []
    def _safe_float(val, default=0.0):
        try:
            return float(val) if val not in (None, "", "None") else default
        except (ValueError, TypeError):
            return default

    def _safe_int(val, default=0):
        try:
            return int(float(val)) if val not in (None, "", "None") else default
        except (ValueError, TypeError):
            return default

    sections: dict[int, dict] = {}
    sort_cols = [c for c in ("section_idx", "item_idx") if c in df.columns]
    if sort_cols:
        try:
            df = df.sort_values(sort_cols)
        except Exception:
            pass
    for _, row in df.iterrows():
        si = _safe_int(row.get("section_idx", 0))
        if si not in sections:
            sections[si] = {
                "paint_class":      str(row.get("paint_class", "A") or "A"),
                "type":             str(row.get("type", "Exterior") or "Exterior"),
                "area_description": str(row.get("area_description", "") or ""),
                "items":            [],
            }
        sections[si]["items"].append({
            "item":      str(row.get("item", "") or ""),
            "method":    str(row.get("method", "Previously painted") or "Previously painted"),
            "area_m2":   _safe_float(row.get("area_m2", 0)),
            "job_notes": str(row.get("job_notes", "") or ""),
        })
    return [sections[k] for k in sorted(sections.keys())]


def get_additional_lines(job_no: str) -> list:
    """Return additional_sections list for the given job from the DB."""
    if not is_configured():
        return []
    df = _read_dataframe("Additional_Lines")
    if df.empty:
        return []
    df = df[df["job_no"].astype(str).str.strip() == str(job_no).strip()]
    if df.empty:
        return []
    if "sort_order" in df.columns:
        try:
            df = df.sort_values("sort_order")
        except Exception:
            pass

    def _safe_float(val, default=0.0):
        try:
            return float(val) if val not in (None, "", "None") else default
        except (ValueError, TypeError):
            return default

    return [
        {
            "item":          str(row.get("item", "") or ""),
            "duration_days": _safe_float(row.get("duration_days", 0)),
            "km":            _safe_float(row.get("km", 0)),
            "liters":        _safe_float(row.get("liters", 0)),
        }
        for _, row in df.iterrows()
    ]
