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

    # Attendance & Bonus (keep if you use them)
    c.execute('''CREATE TABLE IF NOT EXISTS attendance (...)''')
    c.execute('''CREATE TABLE IF NOT EXISTS bonus_log (...)''')

    # Custom Rates + Default Job Notes
    c.execute('''CREATE TABLE IF NOT EXISTS custom_rates (
        item TEXT PRIMARY KEY,
        unit TEXT,
        material REAL,
        labour REAL,
        default_job_notes TEXT,
        date_updated TEXT
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

# ====================== CUSTOM RATES + JOB NOTES ======================
def save_custom_rates(item_rates_dict, item_units_dict, default_job_notes_dict):
    init_local_db()
    conn = get_connection()
    c = conn.cursor()
    date_updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    for sort_order, (item, rates) in enumerate(item_rates_dict.items(), start=1):
        unit = item_units_dict.get(item, "m²")
        notes = default_job_notes_dict.get(item, "")
        c.execute('''INSERT OR REPLACE INTO custom_rates 
                     (item, unit, material, labour, default_job_notes, date_updated, sort_order)
                     VALUES (?, ?, ?, ?, ?, ?, ?)''',
                  (item, unit, rates["material"], rates["labour"], notes, date_updated, sort_order))
    conn.commit()
    conn.close()

def load_custom_rates():
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