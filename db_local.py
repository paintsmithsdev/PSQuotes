import sqlite3
import pandas as pd
from datetime import datetime

DB_FILE = "ppt_database.db"

def get_connection():
    return sqlite3.connect(DB_FILE)

def init_local_db():
    conn = get_connection()
    c = conn.cursor()

    # Clients
    c.execute('''CREATE TABLE IF NOT EXISTS clients (
        client TEXT PRIMARY KEY,
        phone TEXT,
        email TEXT,
        address TEXT,
        date_added TEXT
    )''')

    # Jobs
    c.execute('''CREATE TABLE IF NOT EXISTS jobs (
        job_no TEXT PRIMARY KEY,
        job_name TEXT,
        client TEXT,
        area_manager TEXT,
        team_leader TEXT,
        start_date TEXT,
        total_labour REAL,
        man_days_available REAL,
        status TEXT DEFAULT 'Open',
        date_created TEXT
    )''')

    # Quote Areas
    c.execute('''CREATE TABLE IF NOT EXISTS quote_areas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_no TEXT,
        quote_area TEXT,
        unit TEXT,
        quantity REAL,
        description TEXT,
        prod_qty_per_md REAL
    )''')

    # Attendance
    c.execute('''CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_no TEXT,
        painter_name TEXT,
        emp_id TEXT,
        hourly_rate REAL,
        day1 REAL, day2 REAL, day3 REAL, day4 REAL, day5 REAL,
        day6 REAL, day7 REAL, day8 REAL, day9 REAL, day10 REAL,
        day11 REAL, day12 REAL, day13 REAL, day14 REAL
    )''')

    # Bonus Log
    c.execute('''CREATE TABLE IF NOT EXISTS bonus_log (
        job_no TEXT PRIMARY KEY,
        man_days_available REAL,
        actual_man_days REAL,
        days_saved REAL,
        bonus_rate REAL,
        total_bonus_pool REAL,
        bonus_per_painter REAL
    )''')

    # Custom Rates + Default Job Notes
    c.execute('''CREATE TABLE IF NOT EXISTS custom_rates (
        item TEXT PRIMARY KEY,
        unit TEXT,
        material REAL,
        labour REAL,
        default_job_notes TEXT,
        date_updated TEXT,
        sort_order INTEGER DEFAULT 0
    )''')

    try:
        c.execute("ALTER TABLE custom_rates ADD COLUMN sort_order INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    rows = c.execute("SELECT item FROM custom_rates WHERE sort_order IS NULL OR sort_order = 0 ORDER BY rowid").fetchall()
    for i, (item,) in enumerate(rows, start=1):
        c.execute("UPDATE custom_rates SET sort_order = ? WHERE item = ?", (i, item))

    c.execute('''CREATE TABLE IF NOT EXISTS additional_rates (
        sort_order INTEGER NOT NULL,
        item TEXT NOT NULL,
        rate_unit TEXT NOT NULL,
        rate_value REAL NOT NULL DEFAULT 0,
        date_updated TEXT,
        PRIMARY KEY (sort_order, item, rate_unit)
    )''')

    conn.commit()
    conn.close()

# ====================== CLIENTS ======================
def save_client(client, phone="", email="", address=""):
    init_local_db()
    conn = get_connection()
    c = conn.cursor()
    date_added = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute('''INSERT OR REPLACE INTO clients 
                 (client, phone, email, address, date_added) 
                 VALUES (?, ?, ?, ?, ?)''', 
              (client, phone, email, address, date_added))
    conn.commit()
    conn.close()

def get_all_clients():
    init_local_db()
    conn = get_connection()
    df = pd.read_sql_query("SELECT * FROM clients ORDER BY client", conn)
    conn.close()
    return df

# ====================== JOBS ======================
def save_job(job_no, job_name, client, area_manager, team_leader, start_date, total_labour, man_days_available):
    init_local_db()
    conn = get_connection()
    c = conn.cursor()
    date_created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute('''INSERT OR REPLACE INTO jobs 
                 (job_no, job_name, client, area_manager, team_leader, start_date, total_labour, man_days_available, date_created)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
              (job_no, job_name, client, area_manager, team_leader, str(start_date), total_labour, man_days_available, date_created))
    conn.commit()
    conn.close()

def get_all_jobs():
    init_local_db()
    conn = get_connection()
    df = pd.read_sql_query("SELECT * FROM jobs ORDER BY date_created DESC", conn)
    conn.close()
    return df

# ====================== CUSTOM RATES + JOB NOTES (FIXED) ======================
def save_custom_rates(item_rates_dict, item_units_dict, default_job_notes_dict):
    """Full replace: delete everything first, then insert the current data.
       This makes deletions permanent."""
    init_local_db()
    conn = get_connection()
    c = conn.cursor()
    date_updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. Clear the entire table (this is what was missing!)
    c.execute("DELETE FROM custom_rates")

    # 2. Insert the current data (dict order = display / dropdown order)
    for sort_order, (item, rates) in enumerate(item_rates_dict.items(), start=1):
        unit = item_units_dict.get(item, "m²")
        notes = default_job_notes_dict.get(item, "")
        c.execute('''INSERT INTO custom_rates 
                     (item, unit, material, labour, default_job_notes, date_updated, sort_order)
                     VALUES (?, ?, ?, ?, ?, ?, ?)''',
                  (item, unit, rates["material"], rates["labour"], notes, date_updated, sort_order))

    conn.commit()
    conn.close()


def load_custom_rates():
    """Unchanged — just here for completeness"""
    init_local_db()
    conn = get_connection()
    df = pd.read_sql_query("SELECT * FROM custom_rates ORDER BY sort_order, item", conn)
    conn.close()
    
    item_rates = {}
    item_units = {}
    default_notes = {}
    sort_orders = {}
    for _, row in df.iterrows():
        item = row["item"]
        item_rates[item] = {"material": row["material"], "labour": row["labour"]}
        item_units[item] = row["unit"]
        default_notes[item] = row.get("default_job_notes", "")
        sort_orders[item] = int(row.get("sort_order") or 0)
    return item_rates, item_units, default_notes, sort_orders


# ====================== ADDITIONAL RATES ======================
def save_custom_additional_rates(rows):
    """Full replace: rows are dicts with sort_order, item, rate_unit, rate_value."""
    init_local_db()
    conn = get_connection()
    c = conn.cursor()
    date_updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("DELETE FROM additional_rates")
    for row in rows:
        c.execute(
            """INSERT INTO additional_rates
               (sort_order, item, rate_unit, rate_value, date_updated)
               VALUES (?, ?, ?, ?, ?)""",
            (
                int(row["sort_order"]),
                row["item"],
                row["rate_unit"],
                float(row["rate_value"]),
                date_updated,
            ),
        )
    conn.commit()
    conn.close()


def load_custom_additional_rates():
    init_local_db()
    conn = get_connection()
    df = pd.read_sql_query(
        "SELECT sort_order, item, rate_unit, rate_value FROM additional_rates ORDER BY sort_order, item, rate_unit",
        conn,
    )
    conn.close()
    if df.empty:
        return []
    return [
        {
            "sort_order": int(row["sort_order"]),
            "item": row["item"],
            "rate_unit": row["rate_unit"],
            "rate_value": float(row["rate_value"]),
        }
        for _, row in df.iterrows()
    ]