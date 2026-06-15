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

The script will ask you to paste the spreadsheet URL. It then saves the ID and creates 5 worksheets (Clients, Jobs, Quote_Areas, Attendance, Bonus_Log) with the correct headers.

### e) Share the spreadsheet (optional)

If you want team members to view the raw data in Google Sheets, share the spreadsheet URL with them directly.

## 4) Run the web app

```powershell
streamlit run ppt_job_app.py
```

Then open the local URL shown in the terminal (usually `http://localhost:8501`).

## Notes

- The app works without Google Sheets credentials — cloud features are simply disabled and it runs in session-only mode.
- All PDF generation runs locally in your browser session.
- Never commit `credentials.json` or `sheet_config.json` to version control (both are in `.gitignore`).
