# Pro Paint Teams Job/Site Worksheet App

A Streamlit web app for managing painting jobs end-to-end:

- **Quote Breakdown** – create quotes with auto-calculated man-days
- **Job Spec Sheet** – printable site paperwork for team leaders
- **Attendance & Bonus** – 14-day tracking with automatic bonus calculation
- **Employment Contract** – generate PDF contracts
- **Dashboard** – real-time charts for the current job + historical trends across all jobs
- **Customer Database** – cloud-synced client list, job history, and attendance records via Google Sheets

## 1) Create a virtual environment (recommended)

On Windows (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

## 2) Install dependencies

```powershell
pip install -r requirements.txt
```

## 3) Set up Google Sheets cloud database

The app uses Google Sheets as a cloud database so multiple users can access the same data from any device. Follow these steps once:

### a) Create a Google Cloud service account (free)

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or use an existing one)
3. Go to **APIs & Services > Library** and enable the **Google Sheets API** and **Google Drive API**
4. Go to **APIs & Services > Credentials**
5. Click **Create Credentials > Service Account**
6. Give it a name (e.g. "ppt-job-app") and click through the steps
7. On the service account page, go to **Keys > Add Key > Create new key > JSON**
8. Download the JSON file

### b) Add credentials to the project

1. Rename the downloaded JSON file to `credentials.json`
2. Place it in this project folder (same folder as `ppt_job_app.py`)

### c) Create a Google Sheet in your own Drive

1. Go to [Google Sheets](https://sheets.google.com) and create a **new blank spreadsheet**
2. Name it anything you like (e.g. "PPT Job App Database")
3. Click **Share** and add the service account email as an **Editor**
   - The email is inside `credentials.json`, in the `"client_email"` field
   - It looks like: `something@project-name.iam.gserviceaccount.com`

### d) Run the setup script

```powershell
python setup_sheets.py
```

The script will ask you to paste the spreadsheet URL. It then saves the ID and creates 7 worksheets (`Clients`, `Jobs`, `Quote_Areas`, `Attendance`, `Bonus_Log`, `Custom_Rates`, `Additional_Rates`) with the correct headers.

### e) Share the spreadsheet (optional)

If you want team members to view the raw data in Google Sheets, share the spreadsheet URL with them directly.

## 4) Run the web app

```powershell
python -m streamlit run ppt_job_app.py```

Or double-click `run_app.bat` in the project folder.

Then open the local URL shown in the terminal (usually `http://localhost:8501`).

## Notes

- **Google Sheets is required** for saving clients, jobs, and master rates. Run `setup_sheets.py` once locally, or configure Streamlit Cloud secrets (see below).
- PDFs are generated with **ReportLab** (no Microsoft Word required) — suitable for Streamlit Community Cloud on Linux.
- Word quote export (`.docx`) still uses `template.docx` if present in the app folder.
- Never commit `credentials.json`, `sheet_config.json`, or `email_config.txt` to version control.

## Streamlit Community Cloud secrets

For cloud deploy, add this in **App settings → Secrets** (instead of local files):

```toml
spreadsheet_id = "your-google-sheet-id"

[gcp_service_account]
type = "service_account"
project_id = "your-project"
private_key_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "something@project.iam.gserviceaccount.com"
client_id = "..."
# ... remaining service account fields from credentials.json

[smtp]
sender_email = "quotes@example.com"
password = "your-smtp-password"
smtp_server = "mail.example.com"
smtp_port = "587"
use_tls = "True"
```
