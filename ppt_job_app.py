import hashlib
import json
import re
import streamlit as st
import pandas as pd
import plotly.express as px
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from datetime import date
from io import BytesIO
from urllib.parse import quote as _url_quote
import secrets
import time

import db_sheets
from pdf_linux import generate_letterhead_pdf, generate_quote_pdf

st.set_page_config(page_title="Pro Paint Teams Job/Site Worksheet", layout="wide")

# ---- One-time auth-token store (for new-tab quote links) ----
# Uses st.cache_resource so the dict is a true singleton that survives
# Streamlit script reruns (module-level dicts get reset on every rerun).
_AUTH_TOKEN_TTL = 3600  # seconds (1 hour)

# ---- Persistent session-token store (for page reload auth) ----
_SESSION_TOKEN_TTL = 8 * 3600  # 8 hours in seconds


@st.cache_resource
def _get_auth_token_store() -> dict:
    """Singleton dict: token -> (expiry_monotonic, quote_no). Persists across reruns."""
    return {}


@st.cache_resource
def _get_session_store() -> dict:
    """Singleton dict: session_token -> expiry_monotonic. Persists across reruns."""
    return {}


def _create_auth_token(job_no: str) -> str:
    """Mint a one-time token that grants full auth + opens a specific quote."""
    store = _get_auth_token_store()
    _prune_auth_tokens(store)
    token = secrets.token_urlsafe(32)
    store[token] = (time.monotonic() + _AUTH_TOKEN_TTL, str(job_no))
    return token


def _redeem_auth_token(token: str) -> str | None:
    """Validate a token. Returns quote_no on success, None if invalid/expired.
    Token stays valid until its TTL expires so refreshing the tab keeps working."""
    store = _get_auth_token_store()
    _prune_auth_tokens(store)
    entry = store.get(token)
    if entry and entry[0] > time.monotonic():
        return entry[1]
    return None


def _prune_auth_tokens(store: dict | None = None) -> None:
    if store is None:
        store = _get_auth_token_store()
    now = time.monotonic()
    expired = [k for k, (exp, _) in list(store.items()) if exp <= now]
    for k in expired:
        del store[k]


def _create_session_token() -> str:
    """Mint a long-lived session token and add it to the URL so page reloads
    don't prompt for the password again."""
    store = _get_session_store()
    _prune_session_tokens(store)
    token = secrets.token_urlsafe(32)
    store[token] = time.monotonic() + _SESSION_TOKEN_TTL
    return token


def _validate_session_token(token: str) -> bool:
    """Return True if token exists and has not expired."""
    store = _get_session_store()
    exp = store.get(token)
    if not exp:
        return False
    if time.monotonic() > exp:
        del store[token]
        return False
    return True


def _prune_session_tokens(store: dict | None = None) -> None:
    if store is None:
        store = _get_session_store()
    now = time.monotonic()
    expired = [k for k, exp in list(store.items()) if exp <= now]
    for k in expired:
        del store[k]


def _require_app_password():
    """Block the app until the user enters the password from Streamlit secrets.

    Supports three silent-auth paths that skip the password form:
      1. ?auth_token=  – short-lived token for new-tab quote links.
      2. ?session=     – long-lived (8 h) token written to the URL after login,
                         so hard page reloads don't re-prompt.
      3. session_state – already authenticated in this Streamlit session.
    """
    # --- 1. quote auth_token (new-tab flow) ---
    raw_token = (st.query_params.get("auth_token") or "").strip()
    if raw_token and not st.session_state.get("password_authenticated"):
        quote_no = _redeem_auth_token(raw_token)
        if quote_no:
            st.session_state.password_authenticated = True
            st.session_state["_standalone_quote"] = quote_no
            # Fall through — standalone page block intercepts before st.tabs.
        else:
            st.error("This link has expired or is invalid. Please ask for a new one.")
            st.stop()

    # --- 2. persistent session token (page-reload flow) ---
    raw_session = (st.query_params.get("session") or "").strip()
    if raw_session and not st.session_state.get("password_authenticated"):
        if _validate_session_token(raw_session):
            st.session_state.password_authenticated = True
        else:
            # Token expired — remove it from the URL silently, then fall
            # through to show the password form.
            try:
                del st.query_params["session"]
            except Exception:
                pass

    # --- 3. already authenticated this session ---
    if st.session_state.get("password_authenticated"):
        return

    # --- password form ---
    try:
        correct_password = str(st.secrets["password"])
    except (KeyError, FileNotFoundError, AttributeError):
        st.error("App password not configured. Add `password` to Streamlit secrets.")
        st.stop()
    st.title("Pro Paint Teams Job/Site Worksheet App")
    st.caption("Version 2.8 – Google Sheets + Linux-compatible PDFs")
    with st.form("app_login"):
        entered = st.text_input("Password", type="password")
        if st.form_submit_button("Enter"):
            if entered == correct_password:
                st.session_state.password_authenticated = True
                # Write a session token to the URL so the next reload skips
                # the password form for up to 8 hours.
                sess_token = _create_session_token()
                st.query_params["session"] = sess_token
                st.rerun()
            else:
                st.error("Incorrect password.")
    st.stop()


_require_app_password()

# ---- Standalone quote details page (new-tab auth flow) ----
_standalone_quote = st.session_state.get("_standalone_quote", "")
if _standalone_quote:
    _sa_db_ready = False
    try:
        _sa_db_ready = db_sheets.is_configured()
    except Exception:
        pass

    if not _sa_db_ready:
        st.title(f"Quote Details — {_standalone_quote}")
        st.caption("Pro Paint Teams · authenticated session")
        st.error("Database not configured — cannot load quote details.")
    else:
        try:
            _sa_history = db_sheets.get_job_history()
        except Exception as _e:
            _sa_history = pd.DataFrame()

        if _sa_history.empty:
            st.title(f"Quote Details — {_standalone_quote}")
            st.warning("No saved quotes found.")
        else:
            if "job_no" in _sa_history.columns:
                _sa_history["Quote #"] = _sa_history["job_no"].astype(str)
            _sa_needle = str(_standalone_quote).strip().lower()
            _sa_matches = _sa_history[
                _sa_history["Quote #"].astype(str).str.lower() == _sa_needle
            ]
            if _sa_matches.empty:
                st.title(f"Quote Details — {_standalone_quote}")
                st.warning(f"Quote **{_standalone_quote}** was not found in the database.")
            else:
                _sa_row = _sa_matches.iloc[0]
                _sa_editing = st.session_state.get("_sa_editing", False)
                _sa_loading = (
                    st.session_state.get("_sa_saving", False)
                    or st.session_state.get("_sa_cancelling", False)
                )

                # ---- Header row: title on left, action buttons on right ----
                _sa_hdr_l, _sa_hdr_r = st.columns([5, 1])
                with _sa_hdr_l:
                    st.title(f"Quote Details — {_standalone_quote}")
                    st.caption("Pro Paint Teams · authenticated session")
                with _sa_hdr_r:
                    st.write("")  # vertical alignment spacer
                    if not _sa_editing:
                        if st.button("✏️ Edit", key="sa_pencil_btn", type="secondary",
                                     use_container_width=True, disabled=_sa_loading):
                            st.session_state["_sa_editing"] = True
                            st.rerun()
                    else:
                        if st.button("✕ Cancel", key="sa_cancel_btn", type="secondary",
                                     use_container_width=True, disabled=_sa_loading):
                            st.session_state["_sa_cancelling"] = True
                            st.rerun()

                st.divider()

                _sa_qdate = pd.to_datetime(_sa_row.get("start_date", ""), errors="coerce")
                _sa_saved = pd.to_datetime(_sa_row.get("date_created", ""), errors="coerce")
                try:
                    _sa_labour = f"R{float(_sa_row.get('total_labour', 0) or 0):,.2f}"
                except Exception:
                    _sa_labour = str(_sa_row.get("total_labour", ""))
                try:
                    _sa_md = f"{float(_sa_row.get('man_days_available', 0) or 0):.2f}"
                except Exception:
                    _sa_md = str(_sa_row.get("man_days_available", ""))

                if not _sa_editing:
                    # ---- Read-only view ----
                    _sa_c1, _sa_c2 = st.columns(2)
                    with _sa_c1:
                        st.markdown("**Client**");       st.write(str(_sa_row.get("client", "") or "—"))
                        st.markdown("**Phone**");        st.write(str(_sa_row.get("phone", "") or "—"))
                        st.markdown("**Email**");        st.write(str(_sa_row.get("email", "") or "—"))
                        st.markdown("**Address**");      st.write(str(_sa_row.get("address", "") or "—"))
                        st.markdown("**Area Manager**"); st.write(str(_sa_row.get("area_manager", "") or "—"))
                    with _sa_c2:
                        st.markdown("**Quote Date**")
                        st.write(_sa_qdate.strftime("%Y-%m-%d") if pd.notna(_sa_qdate) else "—")
                        st.markdown("**Status**");       st.write(str(_sa_row.get("status", "") or "—"))
                        st.markdown("**Saved At**")
                        st.write(_sa_saved.strftime("%Y-%m-%d %H:%M") if pd.notna(_sa_saved) else "—")
                        st.markdown("**Labour Total**"); st.write(_sa_labour)
                        st.markdown("**Man-Days**");     st.write(_sa_md)


                else:
                    _sa_saving = st.session_state.get("_sa_saving", False)
                    _sa_cancelling = st.session_state.get("_sa_cancelling", False)

                    # ---- Phase 3: cancel in progress — freeze UI then revert ----
                    if _sa_cancelling:
                        _sa_status_opts = ["Open", "Closed", "Pending", "Cancelled"]
                        _sa_cur_status = str(_sa_row.get("status", "Open") or "Open")
                        _sa_fc1, _sa_fc2 = st.columns(2)
                        with _sa_fc1:
                            st.text_input("Client",       value=str(_sa_row.get("client", "") or ""),       disabled=True)
                            st.text_input("Phone",        value=str(_sa_row.get("phone", "") or ""),        disabled=True)
                            st.text_input("Email",        value=str(_sa_row.get("email", "") or ""),        disabled=True)
                            st.text_input("Address",      value=str(_sa_row.get("address", "") or ""),      disabled=True)
                            st.text_input("Area Manager", value=str(_sa_row.get("area_manager", "") or ""), disabled=True)
                        with _sa_fc2:
                            st.date_input("Quote Date",
                                          value=_sa_qdate.date() if pd.notna(_sa_qdate) else date.today(),
                                          disabled=True)
                            st.selectbox("Status", options=_sa_status_opts,
                                         index=_sa_status_opts.index(_sa_cur_status)
                                         if _sa_cur_status in _sa_status_opts else 0,
                                         disabled=True)
                            st.markdown("**Saved At**")
                            st.write(_sa_saved.strftime("%Y-%m-%d %H:%M") if pd.notna(_sa_saved) else "—")
                            st.markdown("**Labour Total**"); st.write(_sa_labour)
                            st.markdown("**Man-Days**");     st.write(_sa_md)
                        with st.spinner("Cancelling…"):
                            st.session_state["_sa_cancelling"] = False
                            st.session_state["_sa_editing"] = False
                            st.rerun()

                    # ---- Phase 2: perform save with spinner, fields disabled ----
                    elif _sa_saving:
                        _sa_d = st.session_state.get("_sa_edit_data", {})
                        _sa_status_opts = ["Open", "Closed", "Pending", "Cancelled"]
                        _sa_fc1, _sa_fc2 = st.columns(2)
                        with _sa_fc1:
                            st.text_input("Client",       value=_sa_d.get("client", ""),   disabled=True)
                            st.text_input("Phone",        value=_sa_d.get("phone", ""),    disabled=True)
                            st.text_input("Email",        value=_sa_d.get("email", ""),    disabled=True)
                            st.text_input("Address",      value=_sa_d.get("address", ""),  disabled=True)
                            st.text_input("Area Manager", value=_sa_d.get("area_manager", ""), disabled=True)
                        with _sa_fc2:
                            st.date_input("Quote Date", value=_sa_d.get("quote_date", date.today()), disabled=True)
                            st.selectbox("Status", options=_sa_status_opts,
                                         index=_sa_status_opts.index(_sa_d.get("status", "Open"))
                                         if _sa_d.get("status") in _sa_status_opts else 0,
                                         disabled=True)
                            st.markdown("**Saved At**")
                            st.write(_sa_saved.strftime("%Y-%m-%d %H:%M") if pd.notna(_sa_saved) else "—")
                            st.markdown("**Labour Total**"); st.write(_sa_labour)
                            st.markdown("**Man-Days**");     st.write(_sa_md)

                        st.divider()
                        with st.spinner("Saving changes…"):
                            try:
                                _sa_job_no = str(_sa_row.get("job_no", "") or "")

                                # Upsert the client by name (creates if new, updates if exists).
                                db_sheets.save_client(
                                    _sa_d["client"], _sa_d["phone"],
                                    _sa_d["email"], _sa_d["address"])

                                # Update only the editable job fields — leave
                                # total_labour, man_days_available, date_created etc. untouched.
                                db_sheets.update_job_fields(
                                    _sa_job_no,
                                    {
                                        "job_name":     _sa_d["client"],
                                        "client":       _sa_d["client"],
                                        "area_manager": _sa_d["area_manager"],
                                        "start_date":   str(_sa_d["quote_date"]),
                                        "status":       _sa_d["status"],
                                    },
                                )

                                _clear_cloud_cache()
                                st.session_state["_sa_saving"] = False
                                st.session_state["_sa_editing"] = False
                                st.session_state["_standalone_quote"] = _sa_job_no
                                st.rerun()
                            except Exception as _sa_err:
                                st.session_state["_sa_saving"] = False
                                st.error(f"Save failed — {_sheets_error_msg(_sa_err)}")

                    else:
                        # ---- Phase 1: editable form ----
                        # (only reached when neither saving nor cancelling)
                        _sa_status_opts = ["Open", "Closed", "Pending", "Cancelled"]
                        _sa_cur_status = str(_sa_row.get("status", "Open") or "Open")

                        # Callbacks fire BEFORE the rerun, so the loading phase
                        # renders immediately on the very next pass — no editable
                        # flash in between.
                        def _sa_on_save():
                            st.session_state["_sa_saving"] = True
                            st.session_state["_sa_edit_data"] = {
                                "client":       st.session_state.get("_sa_f_client", ""),
                                "phone":        st.session_state.get("_sa_f_phone", ""),
                                "email":        st.session_state.get("_sa_f_email", ""),
                                "address":      st.session_state.get("_sa_f_address", ""),
                                "area_manager": st.session_state.get("_sa_f_am", ""),
                                "quote_date":   st.session_state.get("_sa_f_qdate", date.today()),
                                "status":       st.session_state.get("_sa_f_status", "Open"),
                            }

                        def _sa_on_cancel():
                            st.session_state["_sa_cancelling"] = True

                        with st.form("sa_edit_form"):
                            _sa_fc1, _sa_fc2 = st.columns(2)
                            with _sa_fc1:
                                st.text_input(
                                    "Client", key="_sa_f_client",
                                    value=str(_sa_row.get("client", "") or ""))
                                st.text_input(
                                    "Phone", key="_sa_f_phone",
                                    value=str(_sa_row.get("phone", "") or ""))
                                st.text_input(
                                    "Email", key="_sa_f_email",
                                    value=str(_sa_row.get("email", "") or ""))
                                st.text_input(
                                    "Address", key="_sa_f_address",
                                    value=str(_sa_row.get("address", "") or ""))
                                st.text_input(
                                    "Area Manager", key="_sa_f_am",
                                    value=str(_sa_row.get("area_manager", "") or ""))
                            with _sa_fc2:
                                st.date_input(
                                    "Quote Date", key="_sa_f_qdate",
                                    value=_sa_qdate.date() if pd.notna(_sa_qdate) else date.today())
                                st.selectbox(
                                    "Status", key="_sa_f_status",
                                    options=_sa_status_opts,
                                    index=_sa_status_opts.index(_sa_cur_status)
                                    if _sa_cur_status in _sa_status_opts else 0,
                                )
                                st.markdown("**Saved At**")
                                st.write(_sa_saved.strftime("%Y-%m-%d %H:%M") if pd.notna(_sa_saved) else "—")
                                st.markdown("**Labour Total**"); st.write(_sa_labour)
                                st.markdown("**Man-Days**");     st.write(_sa_md)

                            st.divider()
                            _sa_sf1, _sa_sf2 = st.columns(2)
                            with _sa_sf1:
                                st.form_submit_button(
                                    "💾 Save Changes", type="primary",
                                    use_container_width=True, on_click=_sa_on_save)
                            with _sa_sf2:
                                st.form_submit_button(
                                    "Cancel", type="secondary",
                                    use_container_width=True, on_click=_sa_on_cancel)

    st.stop()

st.title("Pro Paint Teams Job/Site Worksheet App")
st.caption("Version 2.8 – Google Sheets + Linux-compatible PDFs")


@st.cache_data(ttl=300, show_spinner=False)
def _check_sheets_connectivity() -> tuple[bool, str]:
    """Probe Google Sheets connectivity. Result cached for 5 minutes."""
    return db_sheets.health_check()


def _sheets_error_msg(exc: Exception) -> str:
    """Return a plain-language error message for any Google Sheets exception."""
    if isinstance(exc, db_sheets.SheetsUnavailableError):
        return (
            "Google Sheets appears to be temporarily unavailable. "
            "Your session data is preserved — please try again in a few minutes."
        )
    if isinstance(exc, db_sheets.SheetsAuthError):
        return (
            "Google authentication failed. "
            "The service account key may have expired — contact your administrator."
        )
    if isinstance(exc, db_sheets.SheetsConfigError):
        return (
            "Google Sheets is not configured. "
            "Run setup_sheets.py locally or add the required Streamlit secrets."
        )
    return str(exc)


try:
    DB_READY = db_sheets.is_configured()
except Exception:
    DB_READY = False

# Track live connectivity separately from configuration.
# SHEETS_OK=False shows a persistent banner with a Retry button.
SHEETS_OK = True
SHEETS_ERROR_MSG = ""

if DB_READY:
    _conn_ok, _conn_msg = _check_sheets_connectivity()
    if not _conn_ok:
        SHEETS_OK = False
        SHEETS_ERROR_MSG = _conn_msg

if not DB_READY:
    st.warning(
        "⚠️ **Google Sheets not configured** — cloud save is disabled. "
        "Run `setup_sheets.py` locally or add the required secrets on Streamlit Cloud."
    )
elif not SHEETS_OK:
    _banner_col, _retry_col = st.columns([6, 1])
    with _banner_col:
        st.error(
            "🔴 **Google Sheets is currently unavailable.** "
            "Saves and data loads are disabled until the connection is restored. "
            "Your in-session quote data is unaffected. "
            f"_{SHEETS_ERROR_MSG}_"
        )
    with _retry_col:
        st.write("")  # vertical alignment spacer
        if st.button("🔄 Retry", key="sheets_retry_banner", type="secondary",
                     use_container_width=True):
            _check_sheets_connectivity.clear()
            db_sheets._reset_connection_cache()
            st.rerun()

# ====================== HELPER FUNCTIONS ======================

# Cached loaders
@st.cache_data(ttl=120, show_spinner=False)
def _cached_clients(): return db_sheets.get_all_clients()
@st.cache_data(ttl=120, show_spinner=False)
def _cached_jobs(): return db_sheets.get_all_jobs()
@st.cache_data(ttl=120, show_spinner=False)
def _cached_quote_areas(job_no=None): return db_sheets.get_quote_areas(job_no)
@st.cache_data(ttl=120, show_spinner=False)
def _cached_attendance(job_no=None): return db_sheets.get_attendance(job_no)
@st.cache_data(ttl=120, show_spinner=False)
def _cached_bonus_log(): return db_sheets.get_bonus_log()
@st.cache_data(ttl=120, show_spinner=False)
def _cached_job_history():
    jobs = _cached_jobs()
    clients = _cached_clients()
    if jobs.empty:
        return jobs
    if clients.empty or "client" not in clients.columns:
        return jobs
    return jobs.merge(clients, on="client", how="left", suffixes=("", "_client"))

@st.cache_data(ttl=120, show_spinner=False)
def _cached_custom_rates(): return db_sheets.load_custom_rates()
@st.cache_data(ttl=120, show_spinner=False)
def _cached_additional_rates(): return db_sheets.load_custom_additional_rates()

def _clear_cloud_cache():
    _cached_clients.clear()
    _cached_jobs.clear()
    _cached_quote_areas.clear()
    _cached_attendance.clear()
    _cached_bonus_log.clear()
    _cached_job_history.clear()
    _cached_custom_rates.clear()
    _cached_additional_rates.clear()

def _clear_jobs_cache():
    """Clear only job/client caches — leave rates untouched."""
    _cached_clients.clear()
    _cached_jobs.clear()
    _cached_quote_areas.clear()
    _cached_attendance.clear()
    _cached_bonus_log.clear()
    _cached_job_history.clear()

def _clear_rates_cache():
    """Clear only rates caches — leave jobs/clients untouched."""
    _cached_custom_rates.clear()
    _cached_additional_rates.clear()

def _sort_master_rates_df(df):
    """Ensure # column exists, fill gaps, and sort rows by # (dropdown order)."""
    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        return df
    df = df.copy().reset_index(drop=True)
    if "#" not in df.columns:
        df.insert(0, "#", range(1, len(df) + 1))
    df["#"] = pd.to_numeric(df["#"], errors="coerce")
    missing = df["#"].isna()
    if missing.any():
        max_n = df.loc[~missing, "#"].max()
        max_n = int(max_n) if pd.notna(max_n) else 0
        df.loc[missing, "#"] = list(range(max_n + 1, max_n + 1 + int(missing.sum())))
    df["#"] = df["#"].astype(int)
    return df.sort_values("#", kind="stable").reset_index(drop=True)


def _df_to_rate_dicts(df):
    new_rates, new_units, new_notes = {}, {}, {}
    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        return new_rates, new_units, new_notes
    if isinstance(df, pd.DataFrame):
        df = _sort_master_rates_df(df)
    for _, row in df.iterrows():
        item = str(row.get("Item", "")).strip()
        if not item:
            continue
        material = float(row["Material (R/unit)"]) if pd.notna(row.get("Material (R/unit)")) else 0.0
        labour = float(row["Labour (R/unit)"]) if pd.notna(row.get("Labour (R/unit)")) else 0.0
        new_rates[item] = {"material": material, "labour": labour}
        new_units[item] = row.get("Unit", "m²")
        new_notes[item] = str(row.get("Default Job Notes", "") or "")
    return new_rates, new_units, new_notes


def _update_session_rates_from_df(df=None):
    """Populate ITEM_RATES, ITEM_UNITS, DEFAULT_JOB_NOTES from the master rates DataFrame."""
    if df is None:
        df = st.session_state.get("master_rates_df") or st.session_state.get("item_rates_df")
    _mr, _mu, _mn = _df_to_rate_dicts(df)
    st.session_state.ITEM_RATES = _mr
    st.session_state.ITEM_UNITS = _mu
    st.session_state.DEFAULT_JOB_NOTES = _mn


def _clear_streamlit_widget_key(key: str):
    """Drop a widget key so the next run rebuilds it from session data (after save/cancel)."""
    if key in st.session_state:
        del st.session_state[key]


_TAB1_WIDGET_KEYS = (
    "area_code",
    "am_name",
    "am_phone",
    "am_email",
    "client_select",
    "client",
    "client_phone",
    "client_email",
    "client_address",
    "job_no",
    "quote_date",
)


def _queue_quote_open(quote_data: dict) -> None:
    """Stage a saved quote for Tab 1 (applied before Tab 1 widgets on next run)."""
    st.session_state.pending_quote_open = quote_data


def _apply_pending_quote_open() -> bool:
    """Load a queued quote into Tab 1 session state before widgets are created."""
    pending = st.session_state.pop("pending_quote_open", None)
    if not pending:
        return False

    for key in _TAB1_WIDGET_KEYS:
        _clear_streamlit_widget_key(key)
    for key in ("paint_sections", "additional_sections"):
        _clear_streamlit_widget_key(key)

    st.session_state.job_no = str(pending.get("job_no", "") or "")
    st.session_state.client = str(pending.get("client", "") or "")
    st.session_state.client_phone = str(pending.get("phone", "") or "")
    st.session_state.client_email = str(pending.get("email", "") or "")
    st.session_state.client_address = str(pending.get("address", "") or "")
    st.session_state.am_name = str(pending.get("area_manager", "") or "")
    st.session_state.am_phone = str(pending.get("am_phone", "") or "")
    st.session_state.am_email = str(pending.get("am_email", "") or "")
    st.session_state.quote_date = pending.get("quote_date", date.today())

    st.session_state["ppt_nav_tab"] = pending.get(
        "nav_tab", "1. Quote Breakdown (Start Here)"
    )
    return True


def _quote_row_value(row, *keys, default=""):
    for key in keys:
        if key in row.index:
            val = row.get(key)
            if pd.notna(val) and str(val).strip():
                return val
    return default


def _quote_payload_from_display_row(row) -> dict:
    """Build quote-details payload from a quotes display or table row."""
    quote_no = str(_quote_row_value(row, "Quote #", "job_no") or "")
    quote_date_val = _quote_row_value(row, "Quote Date", "start_date")
    parsed_date = pd.to_datetime(quote_date_val, errors="coerce")
    labour_raw = _quote_row_value(row, "Labour Total", "total_labour", default=0)
    man_days_raw = _quote_row_value(row, "Man-Days", "man_days_available", default=0)
    try:
        labour_fmt = f"R{float(labour_raw):,.2f}"
    except (TypeError, ValueError):
        labour_fmt = str(labour_raw)
    try:
        man_days_fmt = f"{float(man_days_raw):.2f}"
    except (TypeError, ValueError):
        man_days_fmt = str(man_days_raw)
    saved_at = _quote_row_value(row, "Saved At", default="")
    if not saved_at:
        saved_at = pd.to_datetime(row.get("date_created", ""), errors="coerce")
        saved_at = saved_at.strftime("%Y-%m-%d %H:%M") if pd.notna(saved_at) else ""
    return {
        "job_no": quote_no,
        "client": str(_quote_row_value(row, "Client", "client") or ""),
        "phone": str(_quote_row_value(row, "Phone", "phone") or ""),
        "email": str(_quote_row_value(row, "Email", "email") or ""),
        "address": str(_quote_row_value(row, "Address", "address") or ""),
        "area_manager": str(_quote_row_value(row, "Area Manager", "area_manager") or ""),
        "quote_date": parsed_date.date() if pd.notna(parsed_date) else date.today(),
        "status": str(_quote_row_value(row, "Status", "status") or ""),
        "saved_at": str(saved_at),
        "labour_total": labour_fmt,
        "man_days": man_days_fmt,
    }


def _find_quote_display_row(display_df: pd.DataFrame, job_no: str):
    if display_df.empty or not str(job_no or "").strip():
        return None
    df = display_df.copy()
    if "Quote #" not in df.columns and "job_no" in df.columns:
        df["Quote #"] = df["job_no"].astype(str)
    needle = str(job_no).strip().lower()
    matches = df[df["Quote #"].astype(str).str.lower() == needle]
    if matches.empty and "job_no" in df.columns:
        matches = df[df["job_no"].astype(str).str.lower() == needle]
    return matches.iloc[0] if not matches.empty else None


def _quote_details_new_tab_href(job_no: str) -> str:
    """Generate a one-time authenticated URL that opens the given quote in a new tab."""
    token = _create_auth_token(str(job_no or "").strip())
    return f"?auth_token={_url_quote(token, safe='')}"


def _clear_quote_url_param() -> None:
    pass  # URL no longer carries quote identity (token was already consumed on load)


def _open_quote_details_from_url(display_df: pd.DataFrame) -> bool:
    """Open quote details when a pending_url_quote has been set by the token-auth flow."""
    job_no = st.session_state.pop("_pending_url_quote", None)
    if not job_no:
        return False
    row = _find_quote_display_row(display_df, job_no)
    if row is None:
        return False
    st.session_state.quote_details_open = True
    st.session_state.quote_details_payload = _quote_payload_from_display_row(row)
    return True


def _apply_tab2_editor_edits(base_df, edited_show, area_only, job_notes_col):
    """Merge data_editor output into tab2_spec_df without wiping in-progress cells."""
    out = base_df.copy()
    n = min(len(out), len(edited_show))
    for col in ("Unit", "Quantity", "Notes"):
        if col in edited_show.columns and col in out.columns and n > 0:
            out.loc[out.index[:n], col] = edited_show.iloc[:n][col].values
    if len(edited_show) > len(out):
        extra = edited_show.iloc[len(out):].copy()
        for col in ("Area Description", "Job Notes"):
            if col not in extra.columns:
                extra[col] = ""
        if "Section" in out.columns:
            start = len(out) + 1
            extra["Section"] = [str(start + i) for i in range(len(extra))]
        out = pd.concat([out, extra.reindex(columns=out.columns, fill_value="")], ignore_index=True)
    elif len(edited_show) < len(out):
        out = out.iloc[: len(edited_show)].copy()
    if "Area Description" in out.columns and len(area_only) == len(out):
        out["Area Description"] = area_only.values[: len(out)]
    if "Job Notes" in out.columns and len(job_notes_col) == len(out):
        out["Job Notes"] = job_notes_col.values[: len(out)]
    return out


def _load_master_rates_dataframe():
    """Load master rates from DB, or built-in defaults when nothing is saved yet."""
    try:
        loaded = _cached_custom_rates()
    except Exception:
        return pd.DataFrame(DEFAULT_MASTER_RATES)
    if len(loaded) == 4:
        custom_rates, custom_units, custom_notes, sort_orders = loaded
    else:
        custom_rates, custom_units, custom_notes = loaded
        sort_orders = {}
    if custom_rates:
        data = [
            {
                "#": sort_orders.get(item) or (i + 1),
                "Item": item,
                "Unit": custom_units.get(item, "m²"),
                "Material (R/unit)": custom_rates[item]["material"],
                "Labour (R/unit)": custom_rates[item]["labour"],
                "Default Job Notes": custom_notes.get(item, ""),
            }
            for i, item in enumerate(custom_rates)
        ]
        return pd.DataFrame(data)
    return pd.DataFrame(DEFAULT_MASTER_RATES)


def _normalize_master_rates_df(df=None):
    """Normalize dtypes and # values without re-sorting (keeps data_editor row indices stable)."""
    if df is None:
        df = _load_master_rates_dataframe()
    cols = ["#", "Item", "Unit", "Material (R/unit)", "Labour (R/unit)", "Default Job Notes"]
    df = df.copy().reset_index(drop=True)
    for col in cols:
        if col not in df.columns:
            if col == "#":
                df[col] = range(1, len(df) + 1)
            else:
                df[col] = "" if col in ("Item", "Unit", "Default Job Notes") else 0.0
    df = df[cols]
    df["Item"] = df["Item"].fillna("").astype(str)
    df["Unit"] = df["Unit"].fillna("m²").astype(str)
    df["Default Job Notes"] = df["Default Job Notes"].fillna("").astype(str)
    df["Material (R/unit)"] = pd.to_numeric(df["Material (R/unit)"], errors="coerce").fillna(0.0)
    df["Labour (R/unit)"] = pd.to_numeric(df["Labour (R/unit)"], errors="coerce").fillna(0.0)
    if "#" not in df.columns:
        df.insert(0, "#", range(1, len(df) + 1))
    df["#"] = pd.to_numeric(df["#"], errors="coerce")
    missing = df["#"].isna()
    if missing.any():
        max_n = df.loc[~missing, "#"].max()
        max_n = int(max_n) if pd.notna(max_n) else 0
        df.loc[missing, "#"] = list(range(max_n + 1, max_n + 1 + int(missing.sum())))
    df["#"] = df["#"].astype(int)
    return df


def _prepare_master_rates_df(df=None):
    """Normalize dtypes/index and sort by # (for load/save, not during live cell edits)."""
    return _sort_master_rates_df(_normalize_master_rates_df(df))


_MASTER_RATES_EDITOR_KEY = "master_rates_editor"


# Default master rates when nothing is saved in the database yet
DEFAULT_MASTER_RATES = [
    {"Item": "Aluminium Restore", "Unit": "each", "Material (R/unit)": 11, "Labour (R/unit)": 35,
     "Default Job Notes": "•	Remove all loose contaminents with a soft bristle brush\n•	Spray Aluminium Cleaner, let soak for 5 to 10min and wipe off\n•	Apply Alu Revivie to a soft fibre cloth and apply in circular motion till dry\n•	Add coates of Alu revive till desired finish is achieved"},
    {"Item": "Ceiling/Soffits", "Unit": "m²", "Material (R/unit)": 47, "Labour (R/unit)": 55,
     "Default Job Notes": "•	Wash ceilings where necessary.\n•	Remove all dust, cobwebs or loose contamination with soft bristle brush.\n•	Caulk ceilings to cornice or wall.\n•	For skimmed ceilings, prime with Midas Plaster Primer.\n•	Apply 2coats of Midafelt 225 (colour to be specified)"},
    {"Item": "Cornices", "Unit": "lm", "Material (R/unit)": 35, "Labour (R/unit)": 35,
     "Default Job Notes": "•	Remove loose contaminents and wash with Midas Degreaser where neccesary\n•	Allow to fully dry, then sand lightly to remove gloss and get a uniform finish.\n•	Caulk cornice where neccesary\n•	Apply 1 coat of Universal Undercoat\n•	Allow 4 hours for overcoating\n•	Apply 2 coats of Midafelt 225 OR 2 coats of Waterbased Non-drip Enamel (colour to be specified)"},
    {"Item": "Crack Repairs", "Unit": "m²", "Material (R/unit)": 30, "Labour (R/unit)": 45,
     "Default Job Notes": "•	Rake out all cracks wider than 0.5mm (not hairline cracks)\n•	Prime with waterproofing slurry kit OR PCT36\n•	Build up cracks with REPAIR MIX\n•	Smoothen or use a sponge to match existing texture\n•	Open all expansion joints and seal with All Round Sealer"},
    {"Item": "Cup Grind- Floor Prep", "Unit": "m²", "Material (R/unit)": 30, "Labour (R/unit)": 45,
     "Default Job Notes": "Note to Client: Cup grinding is a noisy and dusty process. We recommend to remove all items from the room. We aim to complete the work in the prescribed time.\n•	Throuogly sweep floor to remove loose conatminents.\n•	Vaccuum floor to remove all dust and finer contaminents.\n•	Pass the cup-grinder once over the area to achieve a level uniform top.\n•	The objective is to remove the top layer of concrete to create a key coat for the following steps."},
    {"Item": "Cup Grind- To finish with clear sealer", "Unit": "m²", "Material (R/unit)": 75, "Labour (R/unit)": 90,
     "Default Job Notes": "Note to Client: Cup grinding is a noisy and dusty process. We recommend to remove all items from the room. We aim to complete the work in the prescribed time.\n•	Throuogly sweep floor to remove loose conatminents.\n•	Vaccuum floor to remove all dust and finer contaminents.\n•	Pass the cup-grinder once over the area to remove high spots\n•	Sweep and vacuum floor to remove all dust\n•	Using a straight edge, look for high spots and mark with chalk\n•	Pass over with the cup-grinder for a second time to cut a smooth uniform finish. \n•	The objective is to grind down the top to a smooth, flat surface, which can be cleaned and sealed."},
    {"Item": "Exterior Walls Paintwork", "Unit": "m²", "Material (R/unit)": 35, "Labour (R/unit)": 35,
     "Default Job Notes": "•	Preperation work quoted separately\n•	Remove all loose contaminents and let dry after Pressure Wash.\n•	Allow any repair work to fully dry\n•	Sand lightly and remove dust\n•	Spot prime repairs with Masonry Primer. On new build Prime entire wall with Masonry Primer\n•	Apply 2 coats of Midalux 240 (colour to be specified)"},
    {"Item": "Facias/Gutters", "Unit": "lm", "Material (R/unit)": 30, "Labour (R/unit)": 25,
     "Default Job Notes": "•	Ensure surfaces are clean inside and out.\n•	Seal leaks and joints using All Round Sealer, use Peel and Seel for larger gaps\n•	Apply 1 coat of Universal Primer\n•	Apply 2 coats of Midalux 240 (colour to be secified)"},
    {"Item": "High Pressure Washing", "Unit": "each", "Material (R/unit)": 980, "Labour (R/unit)": 700,
     "Default Job Notes": "NOTE TO CLIENT: This is a noisy process and can be messy. This should not take more than the indicted days and will be cleaned up. Please point out water sources to be used to the Team Leader upon commencment of work.\n•	High pressure wash min pressure 200 bar  \n•	Wash to remove all salt and dirt + Loose material from area. \n•	When working on the roof use safety harness and anchor point for safety. \n•	Use drop sheet and garbage bags to collect all loose material generated. \n•	Perform cleanup of the area before commencing to with next step"},
    {"Item": "Interior Skimming", "Unit": "m²", "Material (R/unit)": 75, "Labour (R/unit)": 90,
     "Default Job Notes": "•	Complete repairs first\n•	Allow fillers and repairs to dry\n•	Use a steel trowel and Interior Skimfill to achieve smooth uniform surface\n•	Sand lightly and wipe down before continuing to prime and paint"},
    {"Item": "Interior Walls Paintwork", "Unit": "m²", "Material (R/unit)": 35, "Labour (R/unit)": 35,
     "Default Job Notes": "•	If not skimmed/repaired wash walls with Sugar Soap where contaminated\n•	Let repairs/wash completely dry\n•	Sand repairs and spot prime with Plaster Primer. On new builds prime entire wall with Plaster Primer\n•	Apply two coats of Midafelt 230 (Colour to be specified)"},
    {"Item": "Mould and Fungi Treatment", "Unit": "m²", "Material (R/unit)": 12, "Labour (R/unit)": 35,
     "Default Job Notes": "•	Apply 1 coat Midas FUNGICIDAL WASH to all affected areas.\n•	Allow minimum 24 hours reaction time.\n•	Remove all growth with a stiff fibre brush.\n•	Apply SECOND coat of FUNGICIDAL WASH.\n•	DO NOT rinse off the second coat.\n•	Failure to follow this sequence risks regrowth."},
    {"Item": "Paint Galvanised Metal", "Unit": "m²", "Material (R/unit)": 125, "Labour (R/unit)": 45,
     "Default Job Notes": "•	Lightly sand surface to dull uniform appearance\n•	Apply 1 coat of 504 Surface Tolerant Epoxy as Primer\n•	Allow 6 to 12 hours (weather depending) to dry before overcoating\n•	Apply 1 coat of 504 Surface Tolerant Epoxy\n•	Allow to Allow 6 to 12 hours (weather depending) to dry before overcoating\n•	Apply 2 coats of 112 Solvent Based Acrithane Sealer"},
    {"Item": "Paint Metal", "Unit": "m²", "Material (R/unit)": 75, "Labour (R/unit)": 55,
     "Default Job Notes": "•	Clean metal with Midas Degreaser\n•	Lighlty sand metal to dull uniform surface\n•	Add caulk to frame and wall gaps\n•	Apply 1 coat Metaletch Primer\n•	Apply 1 coat Midaflow Gloss or Midas Masterroof (colour to be specified)"},
    {"Item": "Paint Metal- Windows/Doors", "Unit": "each", "Material (R/unit)": 185, "Labour (R/unit)": 275,
     "Default Job Notes": "•	Clean metal with Midas Degreaser\n•	Lighlty sand metal to dull uniform surface\n•	Add caulk to frame and wall gaps\n•	Apply 1 coat Metaletch Primer\n•	Apply 1 coat Midaflow Gloss or Midas Masterroof (colour to be specified)"},
    {"Item": "Paint Wood", "Unit": "m²", "Material (R/unit)": 60, "Labour (R/unit)": 45,
     "Default Job Notes": "•	If bare/ new wood present, apply Midas Woodprime to all new surfaces\n•	Lightly sand to uniform colour/appearance\n•	Add caulk to frame and wall gaps\n•	Replace cracking/dry or missing putty. Use Putty Hardener\n•	Spot prime nails, screws or metal fittings with Metal Etch Primer\n•	Apply 1 coat of Universal Undercoat\n•	Apply 2 coat of Midalux 240 OR 2 coats of Water Based Non-Drip Enamel (colour to be specified)"},
    {"Item": "Paint Wood- Windows/Doors", "Unit": "each", "Material (R/unit)": 150, "Labour (R/unit)": 225,
     "Default Job Notes": "•	If bare/ new wood present, apply Midas Woodprime to all new surfaces\n•	Lightly sand to uniform colour/appearance\n•	Add caulk to frame and wall gaps\n•	Replace cracking/dry or missing putty. Use Putty Hardener\n•	Spot prime nails, screws or metal fittings with Metal Etch Primer\n•	Apply 1 coat of Universal Undercoat\n•	Apply 2 coat of Midalux 240 OR 2 coats of Midaflow (ext) WB Non-Drip (int) (colour to be specified)"},
    {"Item": "Plaster Repair", "Unit": "m²", "Material (R/unit)": 80, "Labour (R/unit)": 50,
     "Default Job Notes": "•	Remove ALL loose, defective and damaged plaster\n•	Prime area with 1 coat bonding liquid\n•	Repair with Paintsmiths PLASTER REPAIR KIT\n•	Do not exceed 20mm thickness.\n•	Do not rush curing – insufficient curing leads to failure\n•	Wet plaster 2 times daily or cover with dropsheet after first wetting.\n•	Smoothen plaster or use a sponge to match existing texture."},
    {"Item": "Roof Painting", "Unit": "m²", "Material (R/unit)": 55, "Labour (R/unit)": 65,
     "Default Job Notes": "•	Ensure roof is dry and clean. Do not paint in high humidity, temperature or probability of mist/rain.\n•	Spot prime nails, roof screws and metal fittings with Rust Neutrelizer and Metal Etch Primer.\n•	If needed apply 1 coat of Primer\n•	Apply 2 coats of Midas Masteroof OR Rubberduck (colour to be specified)"},
    {"Item": "Skimming", "Unit": "m²", "Material (R/unit)": 75, "Labour (R/unit)": 90,
     "Default Job Notes": "•	Complete repairs first\n•	Allow fillers and repairs to dry\n•	Use a steel trowel and Exterior OR Interior Skimfill to achieve smooth uniform surface\n•	Sand lightly and wipe down before continuing to prime and paint"},
    {"Item": "Skirtings", "Unit": "lm", "Material (R/unit)": 45, "Labour (R/unit)": 35,
     "Default Job Notes": "•	Remove loose contaminents and wash with Midas Degreaser where neccesary\n•	Allow to fully dry, then sand lightly to remove gloss and get a uniform finish.\n•	Caulk skirting where neccesary\n•	Apply 1 coat of Universal Undercoat\n•	Allow 4 hours for overcoating\n•	Apply 2 coats of Midafelt 225 OR 2 coats of Waterbased Non-drip Enamel (colour to be specified)"},
    {"Item": "Tile Remove", "Unit": "m²", "Material (R/unit)": 750, "Labour (R/unit)": 350,
     "Default Job Notes": "•	Skip Rental\n•	Work following tile removal is a provisional part of the quote- Dependant on substrate condition the quote will have to be reassessed."},
    {"Item": "Timber Preserve", "Unit": "m²", "Material (R/unit)": 70, "Labour (R/unit)": 45,
     "Default Job Notes": "•	Lightly sand to uniform colour/appearance\n•	Add caulk to frame and wall gaps\n•	Replace cracking/dry or missing putty. Use Putty Hardener\n•	Apply 1 coat of Timber Preserve**\n•	Lighlty sand and apply second coat of Timber Preserve**\n**Please note drying time is 48hrs +"},
    {"Item": "Timber Preserve- Windows/Doors", "Unit": "each", "Material (R/unit)": 125, "Labour (R/unit)": 175,
     "Default Job Notes": "•	Lightly sand to uniform colour/appearance\n•	Add caulk to frame and wall gaps\n•	Replace cracking/dry or missing putty. Use Putty Hardener\n•	Apply 1 coat of Timber Preserve**\n•	Lighlty sand and apply second coat of Timber Preserve**\n**Please note drying time is 48hrs +"},
    {"Item": "Varnish Wood", "Unit": "m²", "Material (R/unit)": 51, "Labour (R/unit)": 97,
     "Default Job Notes": "•	Lightly sand to uniform colour/appearance\n•	Add caulk to frame and wall gaps\n•	Replace cracking/dry or missing putty. Use Putty Hardener\n•	Apply 1 coat of Indoor/Outdoor Varnish and let dry\n•	Lightly sand and apply second coat of Indoor/Outdoor Varnish"},
    {"Item": "Varnish Wood- Windows/Doors", "Unit": "each", "Material (R/unit)": 175, "Labour (R/unit)": 275,
     "Default Job Notes": "•	Lightly sand to uniform colour/appearance\n•	Add caulk to frame and wall gaps\n•	Replace cracking/dry or missing putty. Use Putty Hardener\n•	Apply 1 coat of Indoor/Outdoor Varnish and let dry\n•	Lightly sand and apply second coat of Indoor/Outdoor Varnish"},
    {"Item": "Waterproofing Rising Damp/ Horizontals", "Unit": "m²", "Material (R/unit)": 55, "Labour (R/unit)": 36,
     "Default Job Notes": "•	Remove all loose contaminants.\n•	Apply 1 coat of Waterpoof Slurry Kit from the floor to +-/ 30cm above the affected area.\n•	Wait 1 to 2 hours and apply second coat of Waterproof Slurry Kit to same area.\n•	Continue with priming and painting."},
    {"Item": "Waterproofing Roofs/Concrete Deks", "Unit": "m²", "Material (R/unit)": 105, "Labour (R/unit)": 55,
     "Default Job Notes": "•	Remove all loose contaminants and materials.\n•	Apply flashmesh and coat with PCT36 to corners.\n•	Apply two coats of PCT36 Slurry. Ligtly wet concrete before application.\n•	Continue with priming and painting. PCT must be overcoated with UV resistant coating."},
    {"Item": "Wood Floors – Sanded to Renew & Varnish", "Unit": "m²", "Material (R/unit)": 105, "Labour (R/unit)": 65,
     "Default Job Notes": "• Note to Client: This is a dusty process. The complete floor must be done in one stage. The floor must be clear of furniture.\n• Vacuum the floor to remove dust.\n• Apply the first coat of indoor varnish thinned with 10% thinners to penetrate the wood.\n• Sand the first coat with 300-grit sandpaper in circular movements and wipe clean.\n• Apply the final coat. Second painter to lay off the wet varnish to prevent lines -maintaining a wet edge."},
    {"Item": "Wood Floors/Rails/ Decks Varnish", "Unit": "m²", "Material (R/unit)": 45, "Labour (R/unit)": 55,
     "Default Job Notes": "• Lightly sand wood to remove loose material and key in the new coat.\n• Apply 2 coats Indoor Varnish for woodwork."},
    {"Item": "Wood Repair", "Unit": "lm", "Material (R/unit)": 50, "Labour (R/unit)": 50,
     "Default Job Notes": "•	Remove damaged coating, loose materials or rotten wood.\n•	Sand down reamining wood to uniform matt finish\n•	Prime all replacement pieces of wood with Wood Primer\n•	Spot prime nails, hinges and fittings with Metal Etch Primer\n•	Treat knots or resin marks with Knotting and Wood sealer\n•	Allow to dry and lightly sand to create a good surface for priming"},
]


_ADDITIONAL_RATE_UNIT_OPTIONS = ["per day", "per km", "per 1000 liters", "per litre"]
_ADDITIONAL_RATE_UNIT_TO_KEY = {
    "per day": "per_day",
    "per km": "per_km",
    "per 1000 liters": "per_1000_liters",
    "per 1000 litres": "per_1000_liters",
    "per litre": "per_litre",
    "per liter": "per_litre",
}


def _rate_unit_to_key(unit: str) -> str:
    key = _ADDITIONAL_RATE_UNIT_TO_KEY.get((unit or "").strip().lower())
    if key:
        return key
    return (unit or "").strip().lower().replace(" ", "_")


# Default additional rates when nothing is saved in the database yet
DEFAULT_MASTER_ADDITIONAL_RATES = [
    {"Additional item": "Travel", "Rate unit": "per day", "R/Rate unit": 8},
    {"Additional item": "Travel", "Rate unit": "per km", "R/Rate unit": 8},
    {"Additional item": "Hiring of Power Washer", "Rate unit": "per day", "R/Rate unit": 1200},
    {"Additional item": "Hiring of Toilet", "Rate unit": "per day", "R/Rate unit": 750},
    {"Additional item": "Skip Hire", "Rate unit": "per day", "R/Rate unit": 450},
    {"Additional item": "Scaffolding Hire", "Rate unit": "per day", "R/Rate unit": 2300},
    {"Additional item": "Water Procurement", "Rate unit": "per 1000 liters", "R/Rate unit": 1500},
    {"Additional item": "Electricity Supply", "Rate unit": "per day", "R/Rate unit": 750},
]


def _sort_master_additional_rates_df(df):
    """Ensure # column exists, fill gaps, and sort rows by #."""
    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        return df
    df = df.copy().reset_index(drop=True)
    if "#" not in df.columns:
        df.insert(0, "#", range(1, len(df) + 1))
    df["#"] = pd.to_numeric(df["#"], errors="coerce")
    missing = df["#"].isna()
    if missing.any():
        max_n = df.loc[~missing, "#"].max()
        max_n = int(max_n) if pd.notna(max_n) else 0
        df.loc[missing, "#"] = list(range(max_n + 1, max_n + 1 + int(missing.sum())))
    df["#"] = df["#"].astype(int)
    return df.sort_values("#", kind="stable").reset_index(drop=True)


def _df_to_additional_rate_dicts(df):
    rates, item_order = {}, []
    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        return rates, item_order
    if isinstance(df, pd.DataFrame):
        df = _sort_master_additional_rates_df(df)
    for _, row in df.iterrows():
        item = str(row.get("Additional item", "")).strip()
        if not item:
            continue
        unit = str(row.get("Rate unit", "")).strip()
        value = float(row["R/Rate unit"]) if pd.notna(row.get("R/Rate unit")) else 0.0
        key = _rate_unit_to_key(unit)
        if item not in rates:
            rates[item] = {}
            item_order.append(item)
        rates[item][key] = value
    return rates, item_order


def _update_session_additional_rates_from_df(df=None):
    if df is None:
        df = st.session_state.get("master_additional_rates_df")
    rates, item_order = _df_to_additional_rate_dicts(df)
    st.session_state.ADDITIONAL_RATES = rates
    st.session_state.ADDITIONAL_ITEM_ORDER = item_order


def _load_master_additional_rates_dataframe():
    try:
        loaded = _cached_additional_rates()
    except Exception:
        return pd.DataFrame(DEFAULT_MASTER_ADDITIONAL_RATES)
    if loaded:
        data = [
            {
                "#": row["sort_order"],
                "Additional item": row["item"],
                "Rate unit": row["rate_unit"],
                "R/Rate unit": row["rate_value"],
            }
            for row in loaded
        ]
        return pd.DataFrame(data)
    return pd.DataFrame(DEFAULT_MASTER_ADDITIONAL_RATES)


def _normalize_master_additional_rates_df(df=None):
    if df is None:
        df = _load_master_additional_rates_dataframe()
    cols = ["#", "Additional item", "Rate unit", "R/Rate unit"]
    df = df.copy().reset_index(drop=True)
    for col in cols:
        if col not in df.columns:
            if col == "#":
                df[col] = range(1, len(df) + 1)
            elif col == "R/Rate unit":
                df[col] = 0.0
            else:
                df[col] = ""
    df = df[cols]
    df["Additional item"] = df["Additional item"].fillna("").astype(str)
    df["Rate unit"] = df["Rate unit"].fillna("per day").astype(str)
    df["R/Rate unit"] = pd.to_numeric(df["R/Rate unit"], errors="coerce").fillna(0.0)
    if "#" not in df.columns:
        df.insert(0, "#", range(1, len(df) + 1))
    df["#"] = pd.to_numeric(df["#"], errors="coerce")
    missing = df["#"].isna()
    if missing.any():
        max_n = df.loc[~missing, "#"].max()
        max_n = int(max_n) if pd.notna(max_n) else 0
        df.loc[missing, "#"] = list(range(max_n + 1, max_n + 1 + int(missing.sum())))
    df["#"] = df["#"].astype(int)
    return df


def _prepare_master_additional_rates_df(df=None):
    return _sort_master_additional_rates_df(_normalize_master_additional_rates_df(df))


def _additional_rates_df_to_db_rows(df):
    rows = []
    for _, row in _sort_master_additional_rates_df(df).iterrows():
        item = str(row.get("Additional item", "")).strip()
        unit = str(row.get("Rate unit", "")).strip()
        if not item or not unit:
            continue
        rows.append(
            {
                "sort_order": int(row["#"]),
                "item": item,
                "rate_unit": unit,
                "rate_value": float(row["R/Rate unit"]) if pd.notna(row.get("R/Rate unit")) else 0.0,
            }
        )
    return rows


_MASTER_ADDITIONAL_RATES_EDITOR_KEY = "master_additional_rates_editor"


def _get_additional_rates():
    if st.session_state.get("ADDITIONAL_RATES"):
        return st.session_state.ADDITIONAL_RATES
    rates, _ = _df_to_additional_rate_dicts(pd.DataFrame(DEFAULT_MASTER_ADDITIONAL_RATES))
    return rates


def _get_additional_item_order():
    order = st.session_state.get("ADDITIONAL_ITEM_ORDER")
    if order:
        return order
    return list(_get_additional_rates().keys())


def _additional_section_cost(sec: dict) -> float:
    item = sec.get("item", "")
    rate = _get_additional_rates().get(item, {})
    if rate.get("per_1000_liters"):
        liters = float(sec.get("liters", 0) or 0)
        return (liters / 1000.0) * rate["per_1000_liters"]
    if rate.get("per_litre"):
        liters = float(sec.get("liters", 0) or 0)
        return liters * rate["per_litre"]
    return (float(sec.get("duration_days", 0) or 0) * rate.get("per_day", 0)) + (
        float(sec.get("km", 0) or 0) * rate.get("per_km", 0)
    )


def _additional_cost_quote_row(sec: dict) -> dict:
    cost = _additional_section_cost(sec)
    item = sec.get("item", "")
    if item == "Water Procurement":
        liters = int(sec.get("liters", 1000) or 0)
        return {
            "description": item,
            "type": f"{liters} liters",
            "amount": f"R{cost:,.2f}",
        }
    return {
        "description": item,
        "type": f"{sec.get('duration_days', 0)} days",
        "amount": sec.get("km", 0),
    }


def _notes_key_sig(text):
    return hashlib.md5((text or "").encode("utf-8", errors="ignore")).hexdigest()[:12]


def _default_paint_item():
    return {"item": "Walls", "method": "Previously painted", "area_m2": 0.0, "job_notes": ""}


def _default_paint_section():
    return {
        "paint_class": "A",
        "type": "Exterior",
        "area_description": "",
        "items": [_default_paint_item()],
    }


def _normalize_paint_sections(sections):
    """Ensure each section has an items list; migrate legacy flat sections."""
    normalized = []
    for sec in sections or []:
        if isinstance(sec.get("items"), list) and sec["items"]:
            normalized.append(sec)
            continue
        normalized.append({
            "paint_class": sec.get("paint_class", "A"),
            "type": sec.get("type", "Exterior"),
            "area_description": sec.get("area_description", ""),
            "items": [{
                "item": sec.get("item", "Walls"),
                "method": sec.get("method", "Previously painted"),
                "area_m2": float(sec.get("area_m2", 0) or 0),
                "job_notes": sec.get("job_notes", ""),
            }],
        })
    return normalized


def _flatten_paint_sections(sections):
    """One merged dict per line item (section fields + item fields)."""
    flat = []
    for sec in _normalize_paint_sections(sections):
        for item in sec.get("items", []):
            flat.append({
                "paint_class": sec.get("paint_class", "A"),
                "type": sec.get("type", "Exterior"),
                "area_description": sec.get("area_description", ""),
                **item,
            })
    return flat


def _tab1_snapshot_id(paint_sections, additional_sections, job_no, client, total_material, total_labour, add_total):
    ps = json.dumps(_flatten_paint_sections(paint_sections), sort_keys=True, default=str)
    ads = json.dumps([{k: s.get(k) for k in ("item", "duration_days", "km", "liters")} for s in additional_sections], sort_keys=True, default=str)
    raw = f"{job_no}|{client}|{total_material:.4f}|{total_labour:.4f}|{add_total:.4f}|{ps}|{ads}"
    return hashlib.md5(raw.encode("utf-8", errors="ignore")).hexdigest()

def _tab2_combined_area_desc(sec: dict) -> str:
    """Area description plus Interior/Exterior type from Tab 1."""
    area_desc = str(sec.get("area_description", "") or "").strip()
    paint_type = str(sec.get("type", "") or "").strip()
    if area_desc and paint_type:
        return f"{area_desc} - {paint_type}"
    return area_desc or paint_type


def _tab2_spec_df_from_paint_sections(paint_sections: list, item_units: dict) -> pd.DataFrame:
    """Builds the Tab 2 spec dataframe from Tab 1 paint sections."""
    rows = []
    for sec_idx, sec in enumerate(_normalize_paint_sections(paint_sections), 1):
        for item_data in sec.get("items", []):
            line = {**sec, **item_data}
            item = line.get("item", "")
            unit = item_units.get(item, line.get("unit", "m²"))
            qty = float(line.get("area_m2", 0) or 0)
            rows.append({
                "Section": str(sec_idx),
                "Area Description": _tab2_combined_area_desc(line),
                "Unit": unit,
                "Quantity": qty,
                "Job Notes": line.get("job_notes", line.get("job_note", "")),
            })
    return pd.DataFrame(rows)


def _tab2_quantity_with_unit(qty, unit: str) -> str:
    """PDF quantity cell: value and unit in one field (e.g. '45.50 m²')."""
    qty_str = f"{float(qty or 0):.2f}"
    unit_str = str(unit or "").strip()
    return f"{qty_str} {unit_str}".strip() if unit_str else qty_str


def _tab2_job_notes_to_steps(text: str) -> list:
    """Split job notes into numbered steps for the Job Spec PDF."""
    steps = []
    for line in str(text or "").replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^[\s•\-\*]+", "", line)
        line = re.sub(r"^\d+[\.\)]\s*", "", line)
        if line:
            steps.append(line)
    return steps


def _tab2_job_spec_section_title(section_num: str, sec: dict, row: pd.Series = None) -> str:
    """Format: '1. ITEM – TYPE – AREA Description'."""
    item = str(sec.get("item", "") or "").strip()
    paint_type = str(sec.get("type", "") or "").strip()
    area_desc = str(sec.get("area_description", "") or "").strip()
    if not area_desc and row is not None:
        area_desc = str(
            row.get("Area Description", row.get("Area Description - Internal/External", "")) or ""
        ).strip()
    title_parts = [p for p in (item, paint_type, area_desc) if p]
    if not title_parts and row is not None:
        title_parts = [str(row.get("Area Description", "") or "").strip()]
    return f"{section_num}. {' – '.join(title_parts)}" if title_parts else str(section_num)


def _tab2_job_spec_sections(paint_sections: list, edited_spec: pd.DataFrame) -> list:
    """Section blocks for Tab 2 Job Spec PDF (no table)."""
    sections = []
    flat_sections = _flatten_paint_sections(paint_sections)
    if edited_spec is not None and not edited_spec.empty:
        for idx, (_, r) in enumerate(edited_spec.iterrows()):
            sec = flat_sections[idx] if idx < len(flat_sections) else {}
            section_num = str(r.get("Section", idx + 1)).replace("Section ", "").strip()
            job_notes = r.get("Job Notes", r.get("Job Note", ""))
            if sec and not str(job_notes or "").strip():
                job_notes = sec.get("job_notes", sec.get("job_note", ""))
            unit = r.get("Unit", sec.get("unit", ""))
            qty = r.get("Quantity", sec.get("area_m2", 0) or 0)
            md = float(r.get("MD /Section", 0) or 0)
            sections.append({
                "title": _tab2_job_spec_section_title(section_num, sec, r),
                "qty_line": (
                    f"{_tab2_quantity_with_unit(qty, unit)} - {md:.2f} Man-days/section"
                ),
                "job_note_steps": _tab2_job_notes_to_steps(job_notes),
                "notes": str(r.get("Notes", "") or ""),
            })
    else:
        for i, sec in enumerate(flat_sections, 1):
            md = float(sec.get("md_section", 0) or 0)
            sections.append({
                "title": _tab2_job_spec_section_title(str(i), sec),
                "qty_line": (
                    f"{_tab2_quantity_with_unit(sec.get('area_m2', 0), sec.get('unit', ''))}"
                    f" - {md:.2f} Man-days/section"
                ),
                "job_note_steps": _tab2_job_notes_to_steps(
                    sec.get("job_notes", sec.get("job_note", ""))
                ),
                "notes": "",
            })
    return sections

def _tab2_area_display_text(area: str, job_notes: str) -> str:
    """Area + job notes in one cell (matches PDF content; formatting on PDF only)."""
    area = str(area or "").strip()
    notes = str(job_notes or "").strip()
    if area and notes:
        return f"{area}\n\n{notes}"
    return area or notes


def _tab2_rows_fingerprint(paint_sections):
    rows = []
    for i, sec in enumerate(_flatten_paint_sections(paint_sections)):
        rows.append((i, sec.get("item"), round(float(sec.get("area_m2", 0) or 0), 4)))
    return hashlib.md5(json.dumps(rows, default=str).encode("utf-8", errors="ignore")).hexdigest()

def _safe_filename_part(text: str, fallback: str = "Unknown") -> str:
    """Sanitize one segment of a download filename."""
    s = re.sub(r'[<>:"/\\|?*]', "", str(text or "").strip())
    s = s.replace(" ", "_")
    return s or fallback

_TAB_EXPORT_FILENAME_SUFFIX = {
    1: None,
    2: "JobSpec",
    3: "Attendance&Bonus",
    4: "EmploymentContract",
}


def _download_filename(
    quote_number,
    client_name,
    extension: str,
    *,
    tab_number: int | None = None,
    extra_suffix: str | None = None,
) -> str:
    """Build download name for tab exports (tabs 1–4 use fixed suffix rules)."""
    ext = extension.lstrip(".")
    name = (
        f"{_safe_filename_part(quote_number, 'Quote')}_"
        f"{_safe_filename_part(client_name, 'Client')}"
    )
    if tab_number in _TAB_EXPORT_FILENAME_SUFFIX:
        suffix = _TAB_EXPORT_FILENAME_SUFFIX[tab_number]
        if suffix:
            name = f"{name}_{suffix}"
    elif extra_suffix:
        name = f"{name}_{_safe_filename_part(extra_suffix, 'Export')}"
    return f"{name}.{ext}"

_TAB_LABELS = [
    "1. Quote Breakdown (Start Here)",
    "2. Job Spec & Site Man-Days",
    "3. Attendance & Bonus",
    "4. Employment Contract",
    "5. Dashboard",
    "6. Quotes",
    "Master Rates",
]

if "ppt_nav_tab" not in st.session_state:
    st.session_state["ppt_nav_tab"] = "1. Quote Breakdown (Start Here)"

tab_quote, tab2, tab3, tab4, tab5, tab6, tab_master = st.tabs(
    _TAB_LABELS, key="ppt_nav_tab"
)

def get_email_config():
    """Load SMTP config from Streamlit secrets or email_config.txt."""
    try:
        if "smtp" in st.secrets:
            s = st.secrets["smtp"]
            config = {k.lower(): str(v) for k, v in dict(s).items()}
            if config.get("sender_email") and config.get("password"):
                if "smtp_server" not in config:
                    config["smtp_server"] = "mail." + config["sender_email"].split("@")[1]
                if "smtp_port" not in config:
                    config["smtp_port"] = "587"
                if "use_tls" not in config:
                    config["use_tls"] = "True"
                return config
    except Exception:
        pass

    config_file = os.path.join(".", "email_config.txt")
    if not os.path.exists(config_file):
        return None
    config = {}
    with open(config_file, "r", encoding="utf-8") as f:
        for line in f:
            if "=" in line:
                key, value = line.strip().split("=", 1)
                config[key.strip().lower()] = value.strip()
    if not all(k in config for k in ["sender_email", "password"]):
        return None
    if "smtp_server" not in config:
        config["smtp_server"] = "mail." + config["sender_email"].split("@")[1]
    if "smtp_port" not in config:
        config["smtp_port"] = "587"
    if "use_tls" not in config:
        config["use_tls"] = "True"
    return config

def send_quote_email(to_email, subject, body, attachment_buf, filename, config):
    """Send email with your custom SMTP settings"""
    msg = MIMEMultipart()
    msg['From'] = config['sender_email']
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    part = MIMEApplication(attachment_buf.getvalue(), Name=filename)
    part['Content-Disposition'] = f'attachment; filename="{filename}"'
    msg.attach(part)

    try:
        smtp_server = config['smtp_server']
        smtp_port = int(config['smtp_port'])
        if config.get('use_ssl', '').lower() == 'true':
            server = smtplib.SMTP_SSL(smtp_server, smtp_port)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port)
            if config.get('use_tls', 'True').lower() == 'true':
                server.starttls()
        server.login(config['sender_email'], config['password'])
        server.send_message(msg)
        server.quit()
        return True, f"✅ Email sent successfully to {to_email}"
    except Exception as e:
        return False, f"❌ Email failed: {str(e)}"

# ====================== TAB 0: MASTER RATES ======================


def _master_rates_column_config():
    return {
        "#": st.column_config.NumberColumn("#", min_value=1, step=1, width="small"),
        "Item": st.column_config.TextColumn("Item", width="medium"),
        "Unit": st.column_config.SelectboxColumn(
            "Unit", options=["m²", "lm", "each", "sum"], default="m²"
        ),
        "Material (R/unit)": st.column_config.NumberColumn(
            "Material (R/unit)", format="R%.2f", min_value=0, default=0.0
        ),
        "Labour (R/unit)": st.column_config.NumberColumn(
            "Labour (R/unit)", format="R%.2f", min_value=0, default=0.0
        ),
        "Default Job Notes": st.column_config.TextColumn("Default Job Notes", width="large"),
    }


def _master_additional_rates_column_config():
    return {
        "#": st.column_config.NumberColumn("#", min_value=1, step=1, width="small"),
        "Additional item": st.column_config.TextColumn("Additional item", width="medium"),
        "Rate unit": st.column_config.SelectboxColumn(
            "Rate unit",
            options=_ADDITIONAL_RATE_UNIT_OPTIONS,
            default="per day",
            width="medium",
        ),
        "R/Rate unit": st.column_config.NumberColumn(
            "R/Rate unit", format="R%.2f", min_value=0, default=0.0
        ),
    }


with tab_master:
    st.header("Master Rates")
    st.caption(
        "Edit rates below — the page will not refresh while you type. "
        "Use # for row order. Click **Save** to sort rows by # and store permanently in the database."
    )

    if "rates_version" not in st.session_state:
        st.session_state.rates_version = 0

    if "master_rates_df" not in st.session_state:
        st.session_state.master_rates_df = _prepare_master_rates_df()
        st.session_state.item_rates_df = st.session_state.master_rates_df.copy()
        _update_session_rates_from_df(st.session_state.master_rates_df)
    elif "#" not in st.session_state.master_rates_df.columns:
        st.session_state.master_rates_df = _prepare_master_rates_df(st.session_state.master_rates_df)

    if "master_additional_rates_df" not in st.session_state:
        st.session_state.master_additional_rates_df = _prepare_master_additional_rates_df()
        _update_session_additional_rates_from_df(st.session_state.master_additional_rates_df)
    elif "#" not in st.session_state.master_additional_rates_df.columns:
        st.session_state.master_additional_rates_df = _prepare_master_additional_rates_df(
            st.session_state.master_additional_rates_df
        )

    st.session_state.pop("master_rates_editor_df", None)
    st.session_state.pop("master_additional_rates_editor_df", None)

    st.subheader("Paint Specification Rates")

    # Form batches all table edits: no script rerun until a submit button is pressed.
    with st.form("master_rates_form", clear_on_submit=False, border=False):
        edited_df = st.data_editor(
            st.session_state.master_rates_df,
            num_rows="dynamic",
            width="stretch",
            hide_index=True,
            key=_MASTER_RATES_EDITOR_KEY,
            column_config=_master_rates_column_config(),
        )
        btn_save, btn_reset, btn_cancel = st.columns(3)
        save_clicked = btn_save.form_submit_button(
            "💾 Save paint specification rates", type="primary", width="stretch"
        )
        reset_clicked = btn_reset.form_submit_button(
            "🔄 Reset paint rates to factory defaults", width="stretch"
        )
        cancel_clicked = btn_cancel.form_submit_button(
            "❌ Cancel / Discard paint rate changes", width="stretch"
        )

    if save_clicked:
        sorted_df = _prepare_master_rates_df(edited_df)
        _mr, _mu, _mn = _df_to_rate_dicts(sorted_df)
        with st.spinner("Saving paint specification rates…"):
            try:
                db_sheets.save_custom_rates(_mr, _mu, _mn)
                _clear_rates_cache()
                st.session_state.master_rates_df = sorted_df
                st.session_state.item_rates_df = sorted_df.copy()
                st.session_state.ITEM_RATES = _mr
                st.session_state.ITEM_UNITS = _mu
                st.session_state.DEFAULT_JOB_NOTES = _mn
                st.session_state.rates_version += 1
                _clear_streamlit_widget_key(_MASTER_RATES_EDITOR_KEY)
                st.success("✅ Paint specification rates saved permanently!")
                st.rerun()
            except Exception as _mr_err:
                st.error(f"Could not save rates — {_sheets_error_msg(_mr_err)}")

    if reset_clicked:
        _mr, _mu, _mn = _df_to_rate_dicts(pd.DataFrame(DEFAULT_MASTER_RATES))
        try:
            db_sheets.save_custom_rates(_mr, _mu, _mn)
            _clear_rates_cache()
            st.session_state.master_rates_df = _prepare_master_rates_df(pd.DataFrame(DEFAULT_MASTER_RATES))
            st.session_state.item_rates_df = st.session_state.master_rates_df.copy()
            _update_session_rates_from_df(st.session_state.master_rates_df)
            st.session_state.rates_version += 1
            _clear_streamlit_widget_key(_MASTER_RATES_EDITOR_KEY)
            st.success("Factory default paint rates restored.")
            st.rerun()
        except Exception as _mr_err:
            st.error(f"Could not reset rates — {_sheets_error_msg(_mr_err)}")

    if cancel_clicked:
        st.session_state.master_rates_df = _prepare_master_rates_df(_load_master_rates_dataframe())
        st.session_state.item_rates_df = st.session_state.master_rates_df.copy()
        _update_session_rates_from_df(st.session_state.master_rates_df)
        st.session_state.rates_version += 1
        _clear_streamlit_widget_key(_MASTER_RATES_EDITOR_KEY)
        st.rerun()

    st.divider()
    st.subheader("Additional Rates")

    with st.form("master_additional_rates_form", clear_on_submit=False, border=False):
        edited_additional_df = st.data_editor(
            st.session_state.master_additional_rates_df,
            num_rows="dynamic",
            width="stretch",
            hide_index=True,
            key=_MASTER_ADDITIONAL_RATES_EDITOR_KEY,
            column_config=_master_additional_rates_column_config(),
        )
        add_btn_save, add_btn_reset, add_btn_cancel = st.columns(3)
        add_save_clicked = add_btn_save.form_submit_button(
            "💾 Save additional rates", type="primary", width="stretch"
        )
        add_reset_clicked = add_btn_reset.form_submit_button(
            "🔄 Reset additional rates to factory defaults", width="stretch"
        )
        add_cancel_clicked = add_btn_cancel.form_submit_button(
            "❌ Cancel / Discard additional rate changes", width="stretch"
        )

    if add_save_clicked:
        sorted_additional_df = _prepare_master_additional_rates_df(edited_additional_df)
        with st.spinner("Saving additional rates…"):
            try:
                db_sheets.save_custom_additional_rates(_additional_rates_df_to_db_rows(sorted_additional_df))
                _clear_rates_cache()
                st.session_state.master_additional_rates_df = sorted_additional_df
                _update_session_additional_rates_from_df(sorted_additional_df)
                st.session_state.rates_version += 1
                _clear_streamlit_widget_key(_MASTER_ADDITIONAL_RATES_EDITOR_KEY)
                st.success("✅ Additional rates saved permanently!")
                st.rerun()
            except Exception as _ar_err:
                st.error(f"Could not save additional rates — {_sheets_error_msg(_ar_err)}")

    if add_reset_clicked:
        default_additional_df = _prepare_master_additional_rates_df(
            pd.DataFrame(DEFAULT_MASTER_ADDITIONAL_RATES)
        )
        try:
            db_sheets.save_custom_additional_rates(_additional_rates_df_to_db_rows(default_additional_df))
            _clear_rates_cache()
            st.session_state.master_additional_rates_df = default_additional_df
            _update_session_additional_rates_from_df(default_additional_df)
            st.session_state.rates_version += 1
            _clear_streamlit_widget_key(_MASTER_ADDITIONAL_RATES_EDITOR_KEY)
            st.success("Factory default additional rates restored.")
            st.rerun()
        except Exception as _ar_err:
            st.error(f"Could not reset additional rates — {_sheets_error_msg(_ar_err)}")

    if add_cancel_clicked:
        st.session_state.master_additional_rates_df = _prepare_master_additional_rates_df(
            _load_master_additional_rates_dataframe()
        )
        _update_session_additional_rates_from_df(st.session_state.master_additional_rates_df)
        st.session_state.rates_version += 1
        _clear_streamlit_widget_key(_MASTER_ADDITIONAL_RATES_EDITOR_KEY)
        st.rerun()

# ====================== TAB 1: QUOTE BREAKDOWN ======================
with tab_quote:
    if _apply_pending_quote_open():
        st.success("Quote loaded from saved records.")

    st.title("Pro Paint Teams Quote")

    # Live rates from Master Rates (single source of truth)
    ITEM_RATES = st.session_state.get("ITEM_RATES", {})
    ITEM_UNITS = st.session_state.get("ITEM_UNITS", {})
    DEFAULT_JOB_NOTES = st.session_state.get("DEFAULT_JOB_NOTES", {})

    # Fallback if Master Rates tab has not populated session yet
    if not ITEM_RATES:
        _rates_df = st.session_state.get("master_rates_df") or st.session_state.get("item_rates_df")
        if _rates_df is None or (isinstance(_rates_df, pd.DataFrame) and _rates_df.empty):
            _rates_df = _prepare_master_rates_df()
            st.session_state.master_rates_df = _rates_df
            st.session_state.item_rates_df = _rates_df
        ITEM_RATES, ITEM_UNITS, DEFAULT_JOB_NOTES = _df_to_rate_dicts(_rates_df)
        st.session_state.ITEM_RATES = ITEM_RATES
        st.session_state.ITEM_UNITS = ITEM_UNITS
        st.session_state.DEFAULT_JOB_NOTES = DEFAULT_JOB_NOTES

    if not st.session_state.get("ADDITIONAL_RATES"):
        _add_rates_df = st.session_state.get("master_additional_rates_df")
        if _add_rates_df is None or (isinstance(_add_rates_df, pd.DataFrame) and _add_rates_df.empty):
            _add_rates_df = _prepare_master_additional_rates_df()
            st.session_state.master_additional_rates_df = _add_rates_df
        _update_session_additional_rates_from_df(_add_rates_df)

    METHOD_MATERIAL_RATES = {'Previously painted': 10, 'New Build': 20}
    CLASS_MULTIPLIERS = {'A': 1.0, 'B': 1.15, 'C': 1.25}

    def calculate_section(section):
        cls = section.get('paint_class', 'A')
        item = section.get('item', 'Walls')
        method = section.get('method', 'Previously painted')
        area = float(section.get('area_m2', 0))
        mult = CLASS_MULTIPLIERS.get(cls, 1.0)

        item_rates = ITEM_RATES.get(item, {'material': 0, 'labour': 0})
        base_material = item_rates['material'] * mult
        base_labour = item_rates['labour'] * mult
        method_material = METHOD_MATERIAL_RATES.get(method, 0) * mult

        material_total = area * (base_material + method_material)
        labour_total = area * base_labour
        return round(material_total, 2), round(labour_total, 2)

    # Area Manager Information
    AREA_MANAGERS = {
        "CS": {"name": "Mirven Julies", "phone": "065 506 0964", "email": "mirven@propaintteams.co.za"},
        "CEM": {"name": "Pieter Visser", "phone": "083 407 7688", "email": "pieter@propaintteams.co.za"},
        "CAW": {"name": "Heinrich Bleuler", "phone": "071 308 5101", "email": "heinrich@propaintteams.co.za"},
    }

    def get_next_quote_number(area_code):
        if not DB_READY: return f"{area_code}001"
        try:
            jobs_df = _cached_jobs()
            if jobs_df.empty: return f"{area_code}001"
            area_jobs = jobs_df[jobs_df["Job No"].astype(str).str.startswith(area_code)]
            if area_jobs.empty: return f"{area_code}001"
            numbers = [int(jno[len(area_code):].strip()) for jno in area_jobs["Job No"].astype(str) if jno.startswith(area_code) and jno[len(area_code):].strip().isdigit()]
            return f"{area_code}{max(numbers)+1:03d}" if numbers else f"{area_code}001"
        except:
            return f"{area_code}001"

    def on_area_change():
        area = st.session_state.get("area_code")
        if area in AREA_MANAGERS:
            st.session_state.am_name = AREA_MANAGERS[area]["name"]
            st.session_state.am_phone = AREA_MANAGERS[area]["phone"]
            st.session_state.am_email = AREA_MANAGERS[area]["email"]
        st.session_state.job_no = get_next_quote_number(area)

    st.subheader("Area Manager Information")
    am_cols = st.columns([1, 2, 2, 3])
    with am_cols[0]:
        st.selectbox("Area", options=list(AREA_MANAGERS.keys()), key="area_code", on_change=on_area_change, index=2)
    with am_cols[1]: area_manager = st.text_input("Area Manager Name", key="am_name")
    with am_cols[2]: am_phone = st.text_input("Phone Number", key="am_phone")
    with am_cols[3]: am_email = st.text_input("Email Address", key="am_email")

    # Client Information
    st.subheader("Client Information")
    client_options = ["— New Client —"]
    if DB_READY:
        try:
            clients_df = _cached_clients()
            if not clients_df.empty:
                client_options += sorted(clients_df["client"].astype(str).unique().tolist())
        except: pass

    def on_client_selected():
        selected = st.session_state.get("client_select")
        if selected == "— New Client —" or not DB_READY: return
        try:
            clients_df = _cached_clients()
            row = clients_df[clients_df["client"] == selected].iloc[0]
            st.session_state.client = row.get("client", selected)
            st.session_state.client_phone = row.get("phone", "")
            st.session_state.client_email = row.get("email", "")
            st.session_state.client_address = row.get("address", "")
        except: pass

    c_cols = st.columns(2)
    with c_cols[0]:
        st.selectbox("Select Existing Client", options=client_options, key="client_select", on_change=on_client_selected, index=0)
        client = st.text_input("Client Name (or edit selected)", key="client")
        client_phone = st.text_input("Phone Number", key="client_phone")
        client_email = st.text_input("Email Address", key="client_email")
    with c_cols[1]:
        client_address = st.text_input("Physical Address", key="client_address")
        job_no = st.text_input("Quote Number", key="job_no")
        quote_date = st.date_input("Date of Quote", key="quote_date")

    # Paint Specification Sections
    st.subheader("Paint Specification Sections")
    if "paint_sections" not in st.session_state:
        st.session_state.paint_sections = [{
            "paint_class": "A", "type": "Exterior", "area_description": "Roofslab",
            "items": [{
                "item": "Waterproofing", "method": "Previously painted", "area_m2": 20.0,
                "job_notes": "",
            }],
        }]
    else:
        st.session_state.paint_sections = _normalize_paint_sections(st.session_state.paint_sections)

    total_material = total_labour = 0.0
    version = st.session_state.get("rates_version", 0)

    item_options = list(ITEM_RATES.keys())
    if not item_options:
        item_options = ["Walls"]

    for i, section in enumerate(st.session_state.paint_sections):
        with st.expander(f"Section {i+1}", expanded=(i == 0)):
            cols = st.columns(2)
            with cols[0]:
                section["paint_class"] = st.selectbox(
                    "Paint Job Class", ["A", "B", "C"], key=f"class_{i}"
                )
            with cols[1]:
                section["type"] = st.selectbox(
                    "Type", ["Interior", "Exterior"], key=f"type_{i}"
                )
            section["area_description"] = st.text_input(
                "Area Description",
                section.get("area_description", ""),
                key=f"desc_{i}",
            )

            if not section.get("items"):
                section["items"] = [_default_paint_item()]

            for j, item in enumerate(section["items"]):
                st.markdown(f"**Item {j + 1}**")
                if item.get("item") not in ITEM_RATES:
                    item["item"] = item_options[0]

                icols = st.columns(3)
                with icols[0]:
                    item["item"] = st.selectbox(
                        "Item", options=item_options, key=f"item_{i}_{j}_v{version}"
                    )
                with icols[1]:
                    item["method"] = st.selectbox(
                        "Method",
                        list(METHOD_MATERIAL_RATES.keys()),
                        key=f"method_{i}_{j}",
                    )
                with icols[2]:
                    unit = ITEM_UNITS.get(item["item"], "m²")
                    item["area_m2"] = st.number_input(
                        f"Area ({unit})",
                        value=float(item.get("area_m2", 0)),
                        step=0.1,
                        key=f"area_m2_{i}_{j}",
                    )

                default_notes = DEFAULT_JOB_NOTES.get(item["item"], "")
                _notes_key = (
                    f"notes_{i}_{j}_{item['item']}_v{version}_{_notes_key_sig(default_notes)}"
                )
                item["job_notes"] = st.text_area(
                    "Job Notes", value=default_notes, key=_notes_key, height=120
                )

                line = {**section, **item}
                mat, lab = calculate_section(line)
                total_material += mat
                total_labour += lab
                st.info(f"**Material:** R{mat:,.2f}  **Labour:** R{lab:,.2f}")

                if len(section["items"]) > 1 and st.button(
                    "🗑️ Remove Item", key=f"rem_item_{i}_{j}"
                ):
                    section["items"].pop(j)
                    st.rerun()

            if st.button("➕ Add Item to Section", key=f"add_item_{i}"):
                section["items"].append(_default_paint_item())
                st.rerun()

            if st.button("🗑️ Remove Section", key=f"rem_p_{i}"):
                st.session_state.paint_sections.pop(i)
                st.rerun()

    if st.button("➕ Add Paint Specification Section"):
        st.session_state.paint_sections.append(_default_paint_section())
        st.rerun()

    # Additional Sections
    st.subheader("Additional Sections")
    if "additional_sections" not in st.session_state:
        st.session_state.additional_sections = []

    add_total = 0.0
    for i, sec in enumerate(st.session_state.additional_sections):
        with st.expander(f"Additional Section {i+1}", expanded=True):
            col1, col2, col3 = st.columns([2, 1.5, 1.5])
            with col1:
                sec["item"] = st.selectbox(
                    "Additional Item",
                    options=_get_additional_item_order(),
                    key=f"add_item_{i}",
                )
            if sec["item"] == "Water Procurement":
                with col2:
                    sec["liters"] = st.number_input(
                        "Quantity (liters)",
                        value=float(sec.get("liters", 1000)),
                        min_value=0.0,
                        step=100.0,
                        key=f"add_liters_{i}",
                    )
                sec["duration_days"] = 0
                sec["km"] = 0
            elif sec["item"] in ["Travel", "Delivery"]:
                with col2:
                    sec["duration_days"] = st.number_input(
                        "Duration (days)",
                        value=sec.get("duration_days", 3),
                        min_value=0,
                        step=1,
                        key=f"add_dur_{i}",
                    )
                with col3:
                    sec["km"] = st.number_input(
                        "Distance (KM)",
                        value=sec.get("km", 80),
                        min_value=0,
                        step=10,
                        key=f"add_km_{i}",
                    )
            else:
                with col2:
                    sec["duration_days"] = st.number_input(
                        "Duration (days)",
                        value=sec.get("duration_days", 1),
                        min_value=0,
                        step=1,
                        key=f"add_dur_{i}",
                    )
                sec["km"] = 0
            cost = _additional_section_cost(sec)
            add_total += cost
            st.success(f"**Cost: R{cost:,.2f}**")
            if st.button("🗑️ Remove Additional", key=f"rem_a_{i}"):
                st.session_state.additional_sections.pop(i)
                st.rerun()

    if st.button("➕ Add Additional Section"):
        st.session_state.additional_sections.append({"item": "Travel", "duration_days": 3, "km": 80})
        st.rerun()

    # Quote Summary
    st.subheader("Quote Summary")
    grand_total = total_material + total_labour + add_total
    col1, col2, col3, col4 = st.columns(4)
    with col1: st.metric("Materials Total", f"R{total_material:,.2f}")
    with col2: st.metric("Labour Total", f"R{total_labour:,.2f}")
    with col3: st.metric("Additional Costs", f"R{add_total:,.2f}")
    with col4: st.metric("**Grand Total**", f"**R{grand_total:,.2f}**")

    # Export to Word
    if st.button("📄 Export to Word – Exact same as index.html", type="primary", width="stretch", key="export_word_btn"):
        try:
            from docxtpl import DocxTemplate
            import io, os
            if not os.path.exists("template.docx"):
                st.error("❌ template.docx not found!")
                st.stop()

            tpl = DocxTemplate("template.docx")

            paint_specs = []
            for sec in _flatten_paint_sections(st.session_state.paint_sections):
                if sec.get("area_m2", 0) <= 0: continue
                mat, lab = calculate_section(sec)
                unit = ITEM_UNITS.get(sec.get("item", ""), "m²")
                paint_specs.append({
                    "type": sec.get("type", ""), "item": sec.get("item", ""), "method": sec.get("method", ""),
                    "converted": str(int(sec.get("area_m2", 0))), "unit": unit,
                    "class": sec.get("paint_class", "A"),
                    "area_description": sec.get("area_description", ""),
                    "job_notes": sec.get("job_notes", ""),
                    "materialcost": f"R{mat:,.2f}", "labourcost": f"R{lab:,.2f}"
                })

            additional_costs = [
                _additional_cost_quote_row(sec) for sec in st.session_state.additional_sections
            ]

            context = {
                "clientname": client, "clientaddress": client_address,
                "clientphone": client_phone, "clientemail": client_email,
                "areaManagerName": area_manager, "areaManagerPhone": am_phone, "areaManagerEmail": am_email,
                "quotedate": quote_date.strftime("%Y-%m-%d"), "quotenumber": job_no,
                "paint_specs": paint_specs, "additional_costs": additional_costs,
                "materialtotal": f"R{total_material:,.2f}", "labourtotal": f"R{total_labour:,.2f}",
                "additionaltotal": f"R{add_total:,.2f}", "grandtotal": f"R{grand_total:,.2f}",
                "grandtotal50": f"R{grand_total*0.5:,.2f}"
            }

            tpl.render(context)
            bio = io.BytesIO()
            tpl.save(bio)
            bio.seek(0)

            st.download_button(
                "✅ Download Quote.docx",
                data=bio.getvalue(),
                file_name=_download_filename(job_no, client, "docx", tab_number=1),
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                width="stretch"
            )
            st.success("✅ Quote exported successfully!")
        except Exception as e:
            st.error(f"Export error: {e}")

    # Email Quote as PDF
    if st.button("📧 Email Quote to Client (as PDF)", type="secondary", width="stretch", key="email_quote_btn"):
        try:
            paint_specs = []
            for sec in _flatten_paint_sections(st.session_state.paint_sections):
                if sec.get("area_m2", 0) <= 0:
                    continue
                mat, lab = calculate_section(sec)
                unit = ITEM_UNITS.get(sec.get("item", ""), "m²")
                paint_specs.append({
                    "type": sec.get("type", ""),
                    "item": sec.get("item", ""),
                    "method": sec.get("method", ""),
                    "converted": str(int(sec.get("area_m2", 0))),
                    "unit": unit,
                    "class": sec.get("paint_class", "A"),
                    "area_description": sec.get("area_description", ""),
                    "job_notes": sec.get("job_notes", ""),
                    "materialcost": f"R{mat:,.2f}",
                    "labourcost": f"R{lab:,.2f}",
                })

            additional_costs = [
                _additional_cost_quote_row(sec) for sec in st.session_state.additional_sections
            ]

            context = {
                "clientname": client,
                "clientaddress": client_address,
                "clientphone": client_phone,
                "clientemail": client_email,
                "areaManagerName": area_manager,
                "areaManagerPhone": am_phone,
                "areaManagerEmail": am_email,
                "quotedate": quote_date.strftime("%Y-%m-%d"),
                "quotenumber": job_no,
                "paint_specs": paint_specs,
                "additional_costs": additional_costs,
                "materialtotal": f"R{total_material:,.2f}",
                "labourtotal": f"R{total_labour:,.2f}",
                "additionaltotal": f"R{add_total:,.2f}",
                "grandtotal": f"R{grand_total:,.2f}",
                "grandtotal50": f"R{grand_total*0.5:,.2f}",
            }

            pdf_bio = generate_quote_pdf(context)

            email_config = get_email_config()
            if not email_config:
                st.error("❌ SMTP not configured. Add email_config.txt or Streamlit secrets.")
                st.stop()

            client_email_addr = client_email.strip()
            if not client_email_addr:
                st.error("Please enter the client's email address first.")
                st.stop()

            subject = f"Pro Paint Teams Quote {job_no} – {client}"
            body = f"""Dear {client},

Please find attached your detailed quotation (Quote {job_no}).

The quote is valid for 14 days. Should you have any questions or wish to proceed, please don’t hesitate to contact me directly.

Best regards,  
{area_manager}  
{am_phone}  
Pro Paint Teams"""

            filename = _download_filename(job_no, client, "pdf", tab_number=1)

            success, message = send_quote_email(
                client_email_addr, subject, body, pdf_bio, filename, email_config
            )

            if success:
                st.success(message)
            else:
                st.error(message)

        except Exception as e:
            st.error(f"PDF / Email error: {e}")

    # Save to Database
    st.divider()
    if DB_READY:
        if st.button("💾 Save Quote & Client to Cloud Database", type="primary", width="stretch", key="save_quote_btn"):
            with st.spinner("Saving quote to cloud…"):
                try:
                    db_sheets.save_client(client, client_phone, client_email, client_address)
                    db_sheets.save_job(job_no=job_no, job_name=client, client=client,
                                       area_manager=area_manager, team_leader="", start_date=quote_date,
                                       total_labour=total_labour, man_days_available=0)
                    _clear_jobs_cache()
                    st.success(f"✅ Quote **{job_no}** saved!")
                except Exception as e:
                    st.error(f"Save failed — {_sheets_error_msg(e)}")

    # Data bridge for other tabs
    st.session_state.total_material = total_material
    st.session_state.total_labour = total_labour
    st.session_state.additional_total = add_total
    st.session_state.grand_total = grand_total
    st.session_state.man_days_available = total_labour / 350 if total_labour > 0 else 0.0
    st.session_state.paint_sections_for_tab2 = st.session_state.paint_sections.copy()
    if "tab1_data" not in st.session_state:
        st.session_state.tab1_data = {}
    _snap = _tab1_snapshot_id(
        st.session_state.paint_sections,
        st.session_state.additional_sections,
        job_no, client, total_material, total_labour, add_total,
    )
    st.session_state.tab1_data.update({
        "job_no": job_no, "client": client, "client_phone": client_phone, "client_email": client_email,
        "client_address": client_address, "area_manager": area_manager, "am_phone": am_phone, "am_email": am_email,
        "quote_date": quote_date, "total_material": total_material, "total_labour": total_labour,
        "additional_total": add_total, "grand_total": grand_total,
        "man_days_available": total_labour / 350 if total_labour > 0 else 0.0,
        "paint_sections": st.session_state.paint_sections.copy(),
        "additional_sections": st.session_state.additional_sections.copy(),
        "snapshot_id": _snap,
    })

# ====================== TAB 2: JOB SPEC & SITE MAN-DAYS ======================
with tab2:
    st.subheader("Job Spec & Site Man-Days")

    data = st.session_state.get("tab1_data", {})
    job_no = data.get("job_no", "Unknown")
    client = data.get("client", "")
    client_phone = data.get("client_phone", "")
    client_email = data.get("client_email", "")
    client_address = data.get("client_address", "")
    area_manager = data.get("area_manager", "")
    am_phone = data.get("am_phone", "")
    am_email = data.get("am_email", "")
    man_days_from_tab1 = float(data.get("man_days_available", 0) or 0)

    # Top client info (matches app display)
    st.markdown(f"""
    <div style="text-align:right;"><b>Job Number:</b> <code>{job_no}</code></div>
    <div><b>Client:</b> {client}&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<b>Phone:</b> {client_phone}&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<b>Email:<b></b> {client_email}&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<b>Address:</b> {client_address}&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<b>Area Manager:</b> {area_manager}</div>
    """, unsafe_allow_html=True)
    st.caption("All information pulled live from Tab 1 Quote Breakdown")

    _iu = st.session_state.get("ITEM_UNITS", {})
    _row_fp = _tab2_rows_fingerprint(data.get("paint_sections", []))

    if st.button("🔄 Pull latest from Tab 1", key="tab2_pull_desc_unique"):
        fresh = _tab2_spec_df_from_paint_sections(data.get("paint_sections", []), _iu)
        st.session_state.tab2_spec_df = fresh
        st.session_state.tab2_last_row_fingerprint = _row_fp
        if "tab2_editor_df" in st.session_state:
            del st.session_state["tab2_editor_df"]
        st.rerun()

    if "tab2_spec_df" not in st.session_state:
        st.session_state.tab2_spec_df = _tab2_spec_df_from_paint_sections(data.get("paint_sections", []), _iu)
        st.session_state.tab2_last_row_fingerprint = _row_fp

    # Build dataframe (keep user edits; only refresh MD from Tab 1)
    df = st.session_state.tab2_spec_df.copy()
    paint_sections = data.get("paint_sections", [])

    if not df.empty:
        flat_paint = _flatten_paint_sections(paint_sections)
        if len(flat_paint) == len(df):
            man_days_list = []
            for i, row in df.iterrows():
                sec = flat_paint[i]
                cls = sec.get('paint_class', 'A')
                item = sec.get('item', 'Walls')
                method = sec.get('method', 'Previously painted')
                area = float(sec.get('area_m2', 0))

                mult = {'A': 1.0, 'B': 1.15, 'C': 1.25}.get(cls, 1.0)
                item_rates = st.session_state.get("ITEM_RATES", {}).get(item, {'labour': 47})
                base_labour = item_rates.get('labour', 47) * mult
                labour_total = area * base_labour
                man_days = round(labour_total / 350, 2) if labour_total > 0 else 0.0
                man_days_list.append(man_days)

            df["MD /Section"] = man_days_list
            st.session_state.tab2_spec_df["MD /Section"] = man_days_list

    # Migrate legacy column names from older session state
    if "Area Description - Internal/External" in df.columns and "Area Description" not in df.columns:
        df = df.rename(columns={"Area Description - Internal/External": "Area Description"})
    if "Quote Area" in df.columns and "Area Description" not in df.columns:
        df = df.rename(columns={"Quote Area": "Area Description"})
    if "Job Note" in df.columns and "Job Notes" not in df.columns:
        df = df.rename(columns={"Job Note": "Job Notes"})
    if "Section" in df.columns:
        df["Section"] = df["Section"].astype(str).str.replace(r"^Section\s+", "", regex=True)

    # Ensure UI column order keeps MD next to Quantity (Job Notes merged into PDF area column)
    preferred_order = [
        "Section",
        "Area Description",
        "Unit",
        "Quantity",
        "MD /Section",
        "Job Notes",
        "Notes",
    ]
    ordered_existing = [c for c in preferred_order if c in df.columns]
    remaining = [c for c in df.columns if c not in ordered_existing]
    df = df[ordered_existing + remaining]

    st.caption("Job notes are edited on Tab 1; they appear under each area in the editor and as numbered steps on the PDF.")

    area_only = df["Area Description"].copy()
    job_notes_col = df["Job Notes"].copy() if "Job Notes" in df.columns else pd.Series([""] * len(df), index=df.index)
    df_show = df.copy()
    df_show["Area Description"] = [
        _tab2_area_display_text(a, n) for a, n in zip(area_only, job_notes_col)
    ]

    @st.fragment
    def _tab2_spec_table_fragment():
        if "tab2_editor_df" not in st.session_state or len(st.session_state.tab2_editor_df) != len(df_show):
            st.session_state.tab2_editor_df = df_show.copy().reset_index(drop=True)
        else:
            view = st.session_state.tab2_editor_df
            for col in ("Section", "Area Description", "MD /Section"):
                if col in df_show.columns and col in view.columns:
                    view[col] = df_show[col].values
            st.session_state.tab2_editor_df = view

        st.session_state.tab2_editor_df = st.data_editor(
            st.session_state.tab2_editor_df,
            num_rows="dynamic",
            width="stretch",
            hide_index=True,
            column_order=(
                "Section",
                "Area Description",
                "Unit",
                "Quantity",
                "MD /Section",
                "Notes",
            ),
            column_config={
                "Section": st.column_config.TextColumn("Nr", disabled=True, width=48),
                "Area Description": st.column_config.TextColumn(
                    "Area Description", disabled=True, width="medium"
                ),
                "Unit": st.column_config.SelectboxColumn(
                    "Unit", options=["m²", "lm", "each", "sum"], width="small"
                ),
                "Quantity": st.column_config.NumberColumn("Quantity", format="%.2f", width="small"),
                "MD /Section": st.column_config.NumberColumn("MD", format="%.2f", disabled=True, width=68),
                "Notes": st.column_config.TextColumn("Notes", width="medium"),
            },
        )
        st.session_state.tab2_spec_df = _apply_tab2_editor_edits(
            st.session_state.tab2_spec_df,
            st.session_state.tab2_editor_df,
            area_only,
            job_notes_col,
        )

    _tab2_spec_table_fragment()
    edited_spec = st.session_state.tab2_spec_df

    st.success(f"**Total Man Days Allowed (from Tab 1):** {man_days_from_tab1:.2f} days")

    spec_sections = _tab2_job_spec_sections(paint_sections, edited_spec)

    # ONE SINGLE PDF DOWNLOAD BUTTON (PDF only - no extra steps)
    if st.button("📄 Download Job Spec PDF (with Letterhead)", type="primary", width="stretch"):
        pdf_buffer = generate_letterhead_pdf(
            tab_title="Job Spec & Site Man-Days",
            job_no=job_no,
            client=client,
            client_info={
                "phone": client_phone,
                "email": client_email,
                "address": client_address,
                "area_manager": area_manager,
                "am_phone": am_phone,
                "am_email": am_email,
            },
            job_spec_sections=spec_sections,
            total_man_days=man_days_from_tab1,
            force_portrait=True
        )
        if pdf_buffer:
            st.download_button(
                "✅ Download Job Spec PDF (Letterhead)",
                data=pdf_buffer.getvalue(),
                file_name=_download_filename(job_no, client, "pdf", tab_number=2),
                mime="application/pdf",
                width="stretch"
            )

# ====================== TAB 3: ATTENDANCE & BONUS ======================
with tab3:
    st.header("3. Attendance & Bonus")
    st.subheader("Site Attendance Sheet")

    data = st.session_state.get("tab1_data", {})
    job_no = data.get("job_no", "—")
    client = data.get("client", "")
    client_address = data.get("client_address", "")

    # ========== UPDATED TOP ROW: Client info (left) + Completed date + Signature (right) ==========
    top_left, top_right = st.columns([5.5, 3.5])
    
    with top_left:
        c1, c2, c3 = st.columns([2.2, 1.3, 2.5])
        with c1:
            st.text_input("Client", value=client, disabled=True, key="t3_client")
        with c2:
            st.text_input("Job", value=job_no, disabled=True, key="t3_job")
        with c3:
            st.text_input("Address", value=client_address, disabled=True, key="t3_address")

    with top_right:
        st.markdown("**Completed Date & Signature**", help="Complete after site work is finished")
        sig_col1, sig_col2 = st.columns(2)
        with sig_col1:
            completed_date = st.date_input(
                "Completed Date", 
                value=date.today(), 
                key="t3_completed_date",
                label_visibility="collapsed"
            )
        with sig_col2:
            signature = st.text_input(
                "Signature", 
                key="t3_signature",
                placeholder="Team Leader sign here",
                label_visibility="collapsed"
            )

    # Man Days Available (kept as before)
    colA, colB = st.columns(2)
    with colA:
        man_days_available = st.number_input(
            "Man Days Allowed", 
            value=float(data.get("man_days_available", 50.0)), 
            step=0.5, 
            key="t3_mda"
        )

    # ==================== 14-ROW ATTENDANCE TABLE (with new R/value column) ====================
    day_cols = [str(i) for i in range(1, 32)]

    if "attendance_df" not in st.session_state or st.session_state.get("attendance_job") != job_no:
        init_data = {
            "Name": [""] * 14,
            **{day: [None] * 14 for day in day_cols},
            "Totals": [None] * 14,
            "R/value": [None] * 14          # NEW COLUMN
        }
        st.session_state.attendance_df = pd.DataFrame(init_data)
        st.session_state.attendance_job = job_no

    @st.fragment
    def _attendance_table_fragment():
        # Keep mdr_rate and totals inside the fragment so they only recompute
        # when attendance data or the rate actually changes — not on every
        # unrelated widget interaction on the page.
        _mdr = st.number_input("MDR Rate (R/day)", value=350.0, step=10.0, key="t3_mdr_rate")

        edited_df = st.data_editor(
            st.session_state.attendance_df,
            num_rows="fixed",
            width="stretch",
            hide_index=True,
            column_config={
                "Name": st.column_config.TextColumn("Name:", width="medium"),
                **{day: st.column_config.NumberColumn(
                    day,
                    min_value=0,
                    max_value=2,
                    step=0.5,
                    format="%.1f",
                    width="small",
                ) for day in day_cols},
                "Totals": st.column_config.NumberColumn("Totals", disabled=True, width="small"),
                "R/value": st.column_config.NumberColumn(
                    "R/value",
                    disabled=True,
                    format="R %.2f",
                    width="small"
                ),
            },
            key="attendance_editor"
        )

        # Compute totals and R/value using vectorised ops — avoid per-row Python loop
        df = edited_df.copy()
        day_numeric = df[day_cols].apply(pd.to_numeric, errors="coerce").fillna(0)
        row_totals = day_numeric.sum(axis=1).round(1)
        df["Totals"] = row_totals
        df["R/value"] = (row_totals * _mdr).round(2)
        _total_used = float(row_totals.sum())

        st.session_state.attendance_df = df
        st.session_state["t3_total_man_days_used"] = _total_used
        st.session_state["t3_mdr_rate_value"] = _mdr

        st.metric("**Man Days Total**", f"{_total_used:.2f}")

    _attendance_table_fragment()

    # Read computed values written by the fragment above
    total_man_days_used = float(st.session_state.get("t3_total_man_days_used", 0.0))
    mdr_rate = float(st.session_state.get("t3_mdr_rate_value", 350.0))

    # ========== UPDATED BOTTOM SUMMARY BLOCK ==========
    st.markdown("### Summary & Bonus Calculation")

    bonus_man_days = max(0.0, man_days_available - total_man_days_used)
    r_value_of_bonus = bonus_man_days * mdr_rate
    bonus_per_man_day = mdr_rate

    sum_col1, sum_col2, sum_col3, sum_col4, sum_col5 = st.columns(5)

    with sum_col1:
        st.metric("Man Days Allowed", f"{man_days_available:.1f}")
    with sum_col2:
        st.metric("Man Days Total", f"{total_man_days_used:.1f}")
    with sum_col3:
        st.metric("Bonus Man Days", f"{bonus_man_days:.1f}")
    with sum_col4:
        st.metric("R value of Bonus", f"R {r_value_of_bonus:,.2f}")
    with sum_col5:
        st.metric("Bonus per Man Day", f"R {bonus_per_man_day:,.2f}")

    # ==================== PDF DOWNLOAD ====================
    if st.button("📄 Generate Attendance PDF", type="primary", width="stretch"):
        att_df = st.session_state.attendance_df
        table_data = [
            ["Date"] + [""] * 31 + ["", ""],
            ["Name:"] + [str(i) for i in range(1, 32)] + ["Totals", "R/value"],
        ]
        for i in range(14):
            row = [str(att_df.iloc[i]["Name"] or "")] + [
                str(att_df.iloc[i][str(d)]) if pd.notna(att_df.iloc[i][str(d)]) else ""
                for d in range(1, 32)
            ] + ["", ""]  # Totals and R/value left blank on PDF for manual fill-in
            table_data.append(row)

        pdf_buffer = generate_letterhead_pdf(
            tab_title="Attendance",
            job_no=job_no,
            client=client,
            attendance_meta={
                "client": client,
                "job_no": job_no,
                "address": client_address,
                "man_days_allowed": man_days_available,
                "man_days_total": total_man_days_used,
                "bonus_man_days": bonus_man_days,
                "r_value_of_bonus": r_value_of_bonus,
                "bonus_per_man_day": bonus_per_man_day,
                "mdr_rate": mdr_rate,
                "signature": signature,
            },
            table_rows=table_data,
            force_portrait=False,
            attendance_table=True,
            template_candidates=[
                "attendance_template.docx",
                "Attendance_template.docx",
                "ATTENDANCE_TEMPLATE.docx",
                "template.docx",
                "Template.docx",
                "letterhead.docx",
                "Letterhead.docx",
            ],
        )

        if pdf_buffer:
            st.session_state["attendance_pdf_bytes"] = pdf_buffer.getvalue()
            st.success("Attendance PDF generated. Click download below.")

    if st.session_state.get("attendance_pdf_bytes"):
        st.download_button(
            label="⬇️ Download Attendance.pdf",
            data=st.session_state["attendance_pdf_bytes"],
            file_name=_download_filename(job_no, client, "pdf", tab_number=3),
            mime="application/pdf",
            width="stretch"
        )
# ====================== TAB 4: EMPLOYMENT CONTRACT ======================
with tab4:
    st.subheader("Employment Contract Generator")
    st.info("Fill in employee details below, then download a PDF contract.")

    data = st.session_state.get("tab1_data", {})
    _job_mgr_key = f"{data.get('job_no', '')}|{data.get('area_manager', '')}"
    if st.session_state.get("_tab4_job_mgr_key") != _job_mgr_key:
        st.session_state._tab4_job_mgr_key = _job_mgr_key
        st.session_state.emp_site = str(data.get("job_no", "") or "")
        st.session_state.emp_mgr = str(data.get("area_manager", "") or "")
    elif "emp_site" not in st.session_state:
        st.session_state.emp_site = str(data.get("job_no", "") or "")
        st.session_state.emp_mgr = str(data.get("area_manager", "") or "")
    st.caption(f"Tab 1: **{data.get('job_no', '—')}** — {data.get('client', '')}")

    ec1, ec2 = st.columns(2)
    with ec1:
        emp_name = st.text_input("Employee Full Name", "", key="emp_name")
        emp_id = st.text_input("SA ID / Passport No", "", key="emp_id")
        emp_role = st.selectbox("Role", ["Painter", "Team Leader", "Assistant", "Labourer", "Other"], key="emp_role")
        emp_rate = st.number_input("Daily Rate (R)", value=350.0, step=10.0, key="emp_rate")
    with ec2:
        emp_start = st.date_input("Contract Start Date", date.today(), key="emp_start")
        emp_end = st.date_input("Contract End Date", date.today(), key="emp_end")
        emp_site = st.text_input("Site / Job Name", key="emp_site")
        emp_manager = st.text_input("Supervisor / Manager", key="emp_mgr")

    contract_text = f"""
EMPLOYMENT CONTRACT – PRO PAINT TEAMS

Employee: {emp_name}
ID/Passport: {emp_id}
Role: {emp_role}
Daily Rate: R{emp_rate:,.2f}

Site/Job: {emp_site}
Contract Period: {emp_start} to {emp_end}
Supervisor: {emp_manager}

TERMS AND CONDITIONS:
1. The employee agrees to perform all duties related to the role of {emp_role}.
2. Working hours: 07:00 – 16:30 (Mon–Fri), with a 30-minute lunch break.
3. The employee will be paid at the agreed daily rate for each day worked.
4. PPE must be worn at all times on site.
5. The employee must follow all health and safety regulations.
6. Either party may terminate this contract with 1 day written notice.
7. Tools and materials remain the property of Pro Paint Teams.
8. Bonus payments are discretionary and based on Man-Days saved.

Signed (Employee): ____________________________  Date: ____________

Signed (Manager):  ____________________________  Date: ____________
"""
    st.text_area("Contract Preview", contract_text, height=400, disabled=True)

    contract_lines = [line.strip() for line in contract_text.strip().split("\n") if line.strip()]
    pdf_buffer = generate_letterhead_pdf(
        "Employment Contract",
        data.get("job_no", ""),
        data.get("client", ""),
        content_lines=contract_lines
    )
    if pdf_buffer:
        st.download_button(
            "📄 Download Employment Contract PDF",
            data=pdf_buffer.getvalue(),
            file_name=_download_filename(
                data.get("job_no", ""), data.get("client", ""), "pdf", tab_number=4
            ),
            mime="application/pdf",
            width="stretch",
            type="primary"
        )

# ====================== TAB 5: DASHBOARD ======================
with tab5:
    st.subheader("Job Dashboard")

    data = st.session_state.get("tab1_data", {})
    man_days_available = float(data.get("man_days_available", 0) or 0)
    paint_sections = data.get("paint_sections", [])
    flat_paint_sections = _flatten_paint_sections(paint_sections)
    has_line_items = any(float(s.get("area_m2", 0) or 0) > 0 for s in flat_paint_sections)

    st.info(
        f"**Tab 1** — Job: **{data.get('job_no', '—')}** | Client: **{data.get('client', '')}** | "
        f"Materials: **R{float(data.get('total_material', 0) or 0):,.2f}** | Labour: **R{float(data.get('total_labour', 0) or 0):,.2f}** | "
        f"Grand total: **R{float(data.get('grand_total', 0) or 0):,.2f}**"
    )

    if not paint_sections or not has_line_items:
        st.warning("Add at least one paint section with a quantity greater than zero on Tab 1.")
    else:
        df_spec_dash = pd.DataFrame([
            {
                "Quote Area": sec.get("item", "Unknown"),
                "Quantity": float(sec.get("area_m2", 0)),
                "Allowed Man-Days": (float(sec.get("area_m2", 0)) / 30)
            } for sec in flat_paint_sections if float(sec.get("area_m2", 0)) > 0
        ])

        d1, d2, d3 = st.columns(3)
        with d1: st.metric("Man-days (Tab 1 labour ÷ 350)", f"{man_days_available:.2f}")
        with d2:
            total_qty = df_spec_dash["Quantity"].sum()
            st.metric("Total quoted quantity (sum of units)", f"{total_qty:,.0f}")
        with d3: st.metric("Line items", f"{len(df_spec_dash)}")

        if not df_spec_dash.empty:
            fig_bar = px.bar(df_spec_dash, x="Quote Area", y="Allowed Man-Days", title="Allowed Man-Days per Area", color="Allowed Man-Days", color_continuous_scale="Blues")
            fig_bar.update_layout(xaxis_tickangle=-45)
            st.plotly_chart(fig_bar, width="stretch", key="dash_bar_chart")

            fig_pie = px.pie(df_spec_dash, names="Quote Area", values="Allowed Man-Days", title="Man-Day Distribution by Area")
            st.plotly_chart(fig_pie, width="stretch", key="dash_pie_chart")

            fig_qty = px.bar(df_spec_dash, x="Quote Area", y="Quantity", title="Quantity per Area", color="Quote Area")
            fig_qty.update_layout(xaxis_tickangle=-45, showlegend=False)
            st.plotly_chart(fig_qty, width="stretch", key="dash_qty_chart")

        if st.button("📄 Download Dashboard Summary PDF", type="primary", width="stretch",
                     key="dash_pdf_btn"):
            dash_rows = [["Quote Area", "Quantity", "Allowed Man-Days"]]
            for _, r in df_spec_dash.iterrows():
                dash_rows.append([str(r.get("Quote Area", "")), f"{r.get('Quantity', 0):.1f}", f"{r.get('Allowed Man-Days', 0):.2f}"])
            content_lines = [
                f"Materials: R{data.get('total_material', 0):,.2f}",
                f"Labour: R{data.get('total_labour', 0):,.2f}",
                f"Grand Total: R{data.get('grand_total', 0):,.2f}",
                f"Man-days Available: {float(data.get('man_days_available', 0)):.2f}"
            ]
            pdf_buffer = generate_letterhead_pdf(
                "Job Dashboard Summary",
                data.get("job_no", ""),
                data.get("client", ""),
                content_lines,
                table_rows=dash_rows
            )
            if pdf_buffer:
                st.download_button(
                    "✅ Download Dashboard Summary PDF",
                    data=pdf_buffer.getvalue(),
                    file_name=_download_filename(
                        data.get("job_no", ""), data.get("client", ""), "pdf", extra_suffix="Dashboard"
                    ),
                    mime="application/pdf",
                    width="stretch",
                )

# ====================== TAB 6: QUOTES ======================
with tab6:
    _t6_hdr, _t6_btn, _t6_spacer = st.columns([3, 1, 6])
    with _t6_hdr:
        st.subheader("Saved Quotes")
    with _t6_btn:
        st.write("")  # vertical alignment spacer
        if st.button("🔄 Refetch", key="quotes_refetch_btn",
                     type="secondary", use_container_width=True):
            _cached_job_history.clear()
            st.rerun()

    if not DB_READY:
        st.warning("Google Sheets is not configured, so saved quotes cannot be loaded.")
    else:
        try:
            quotes_df = _cached_job_history()
        except Exception as _t6_err:
            _t6_err_col, _t6_retry_col = st.columns([5, 1])
            with _t6_err_col:
                st.error(
                    f"Could not load saved quotes — {_sheets_error_msg(_t6_err)}"
                )
            with _t6_retry_col:
                if st.button("🔄 Retry", key="t6_load_retry_btn", type="secondary",
                             use_container_width=True):
                    _cached_job_history.clear()
                    _check_sheets_connectivity.clear()
                    db_sheets._reset_connection_cache()
                    st.rerun()
            quotes_df = pd.DataFrame()

        if quotes_df.empty:
            st.info("No saved quotes found yet. Save a quote on Tab 1 to see it here.")
        else:
            display_df = quotes_df.copy()

            if "job_no" in display_df.columns:
                display_df["Quote #"] = display_df["job_no"].astype(str)
            else:
                display_df["Quote #"] = ""

            if "date_created" in display_df.columns:
                display_df["Saved At"] = pd.to_datetime(
                    display_df["date_created"], errors="coerce"
                ).dt.strftime("%Y-%m-%d %H:%M")
                display_df["Saved At"] = display_df["Saved At"].fillna("")
            else:
                display_df["Saved At"] = ""

            if "total_labour" in display_df.columns:
                display_df["Labour Total"] = pd.to_numeric(
                    display_df["total_labour"], errors="coerce"
                ).fillna(0.0)
            else:
                display_df["Labour Total"] = 0.0

            if "man_days_available" in display_df.columns:
                display_df["Man-Days"] = pd.to_numeric(
                    display_df["man_days_available"], errors="coerce"
                ).fillna(0.0)
            else:
                display_df["Man-Days"] = 0.0

            if "start_date" in display_df.columns:
                display_df["Quote Date"] = pd.to_datetime(
                    display_df["start_date"], errors="coerce"
                ).dt.strftime("%Y-%m-%d")
                display_df["Quote Date"] = display_df["Quote Date"].fillna("")
            else:
                display_df["Quote Date"] = ""

            for col in ["client", "phone", "email", "address", "area_manager", "status"]:
                if col not in display_df.columns:
                    display_df[col] = ""


            # ---------- Filters ----------
            f1, f2, f3 = st.columns(3)
            with f1:
                search_text = st.text_input(
                    "Search (Quote # / Client / Email)",
                    value="",
                    key="quotes_search_text",
                    placeholder="Type quote number, client, or email",
                ).strip()
            with f2:
                status_options = ["All"] + sorted(
                    [s for s in display_df["status"].astype(str).fillna("").unique().tolist() if s]
                )
                status_filter = st.selectbox(
                    "Status filter",
                    options=status_options if status_options else ["All"],
                    index=0,
                    key="quotes_status_filter",
                )
            with f3:
                client_list = sorted(
                    [c for c in display_df["client"].astype(str).fillna("").unique().tolist() if c]
                )
                client_filter = st.selectbox(
                    "Client filter",
                    options=["All"] + client_list,
                    index=0,
                    key="quotes_client_filter",
                )

            filtered_df = display_df.copy()
            if search_text:
                query = search_text.lower()
                search_mask = (
                    filtered_df["Quote #"].astype(str).str.lower().str.contains(query, na=False)
                    | filtered_df["client"].astype(str).str.lower().str.contains(query, na=False)
                    | filtered_df["email"].astype(str).str.lower().str.contains(query, na=False)
                )
                filtered_df = filtered_df[search_mask]
            if status_filter != "All":
                filtered_df = filtered_df[
                    filtered_df["status"].astype(str).str.lower()
                    == str(status_filter).strip().lower()
                ]
            if client_filter != "All":
                filtered_df = filtered_df[
                    filtered_df["client"].astype(str).str.lower()
                    == str(client_filter).strip().lower()
                ]

            table_df = filtered_df[
                [
                    "Quote #",
                    "client",
                    "phone",
                    "email",
                    "address",
                    "area_manager",
                    "Quote Date",
                    "Labour Total",
                    "Man-Days",
                    "status",
                    "Saved At",
                ]
            ].rename(
                columns={
                    "client": "Client",
                    "phone": "Phone",
                    "email": "Email",
                    "address": "Address",
                    "area_manager": "Area Manager",
                    "status": "Status",
                }
            )

            table_df = table_df.sort_values(
                by=["Saved At", "Quote #"], ascending=[False, False], na_position="last"
            ).reset_index(drop=True)
            table_df.insert(0, "Row", range(1, len(table_df) + 1))
            table_df["Labour Total"] = table_df["Labour Total"].map(lambda x: f"R{x:,.2f}")
            table_df["Man-Days"] = table_df["Man-Days"].map(lambda x: f"{x:.2f}")

            csv_df = display_df.copy()
            csv_df = csv_df.sort_values(
                by=["Saved At", "Quote #"], ascending=[False, False], na_position="last"
            ).reset_index(drop=True)
            csv_export_df = csv_df[
                [
                    "Quote #",
                    "client",
                    "phone",
                    "email",
                    "address",
                    "area_manager",
                    "Quote Date",
                    "total_labour",
                    "man_days_available",
                    "status",
                    "Saved At",
                ]
            ].rename(
                columns={
                    "client": "Client",
                    "phone": "Phone",
                    "email": "Email",
                    "address": "Address",
                    "area_manager": "Area Manager",
                    "total_labour": "Labour Total",
                    "man_days_available": "Man-Days",
                    "status": "Status",
                }
            )
            st.download_button(
                "⬇️ Export All Quotes (CSV)",
                data=csv_export_df.to_csv(index=False).encode("utf-8"),
                file_name="saved_quotes_all.csv",
                mime="text/csv",
                type="secondary",
                width="content",
            )

            total_rows = len(table_df)
            c1, c2, c3 = st.columns([1, 1, 2])
            with c1:
                page_size = int(
                    st.selectbox(
                        "Rows per page",
                        options=[10, 20, 50, 100],
                        index=1,
                        key="quotes_page_size",
                    )
                )
            total_pages = max(1, (total_rows + page_size - 1) // page_size)
            with c2:
                page = int(
                    st.number_input(
                        "Page",
                        min_value=1,
                        max_value=total_pages,
                        value=1,
                        step=1,
                        key="quotes_page_number",
                    )
                )
            with c3:
                st.caption(f"Showing page {page} of {total_pages} ({total_rows} saved quotes)")

            start_idx = (page - 1) * page_size
            end_idx = start_idx + page_size
            page_df = table_df.iloc[start_idx:end_idx].copy()

            col_widths = [0.4, 1.0, 1.6, 1.2, 1.1, 1.1, 0.9, 1.1]
            header_cols = st.columns(col_widths)
            header_cols[0].markdown("**Row**")
            header_cols[1].markdown("**Quote #**")
            header_cols[2].markdown("**Client**")
            header_cols[3].markdown("**Quote Date**")
            header_cols[4].markdown("**Labour Total**")
            header_cols[5].markdown("**Man-Days**")
            header_cols[6].markdown("**Status**")
            header_cols[7].markdown("**Details/Edit**")

            for i, row in page_df.iterrows():
                row_cols = st.columns(col_widths)
                row_cols[0].write(int(row.get("Row", 0)))
                row_cols[1].write(str(row.get("Quote #", "")))
                row_cols[2].write(str(row.get("Client", "")))
                row_cols[3].write(str(row.get("Quote Date", "")))
                row_cols[4].write(str(row.get("Labour Total", "")))
                row_cols[5].write(str(row.get("Man-Days", "")))
                row_cols[6].write(str(row.get("Status", "")))

                quote_no = str(row.get("Quote #", "") or "")
                if quote_no:
                    _tab6_token = _create_auth_token(quote_no)
                    _tab6_url = f"?auth_token={_url_quote(_tab6_token, safe='')}"
                    row_cols[7].markdown(
                        f'<a href="{_tab6_url}" target="_blank" rel="noopener noreferrer"'
                        f' style="display:inline-block;width:100%;text-align:center;'
                        f'padding:0.35rem 0.5rem;border-radius:0.35rem;'
                        f'border:1px solid rgba(49,51,63,0.2);'
                        f'color:inherit;font-size:0.875rem;'
                        f'text-decoration:none;white-space:nowrap;">'
                        f"Details/Edit</a>",
                        unsafe_allow_html=True,
                    )