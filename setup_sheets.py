"""
One-time setup script for the PPT Job App Google Sheets database.

Run this after placing your credentials.json in the project folder:
    python setup_sheets.py

It will walk you through:
  1. Creating a Google Sheet in YOUR Google Drive (not the service account's)
  2. Sharing it with the service account
  3. Saving the spreadsheet ID locally
  4. Adding the 5 required worksheets with correct headers
"""

import json
import os
import db_sheets


def _extract_sheet_id(url_or_id):
    """Accept a full Google Sheets URL or just the ID."""
    url_or_id = url_or_id.strip()
    if "/spreadsheets/d/" in url_or_id:
        part = url_or_id.split("/spreadsheets/d/")[1]
        return part.split("/")[0]
    return url_or_id


def main():
    if not os.path.isfile(db_sheets._creds_path()):
        print("ERROR: credentials.json not found.")
        print("Place your Google Cloud service account credentials.json in this folder.")
        print("See README.md for setup instructions.")
        return

    sa_email = db_sheets.get_service_account_email()
    print("=" * 60)
    print("  PPT Job App – Google Sheets Setup")
    print("=" * 60)
    print()
    print("STEP 1: Create a new Google Sheet")
    print("  Go to https://sheets.google.com and click '+ Blank'")
    print("  Name it anything you like (e.g. 'PPT Job App Database')")
    print()
    print("STEP 2: Share it with the service account")
    print(f"  Click 'Share', then add this email as an Editor:")
    print(f"    {sa_email}")
    print()
    print("STEP 3: Copy the spreadsheet URL or ID")
    print("  The URL looks like:")
    print("  https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit")
    print()

    user_input = input("Paste the spreadsheet URL or ID here: ").strip()
    if not user_input:
        print("No input provided. Exiting.")
        return

    sheet_id = _extract_sheet_id(user_input)
    print(f"\nSpreadsheet ID: {sheet_id}")

    db_sheets.save_spreadsheet_id(sheet_id)
    print("Saved to sheet_config.json")

    print("\nConnecting and creating worksheets...")
    try:
        sp = db_sheets.connect()
        print(f"Connected to: {sp.title}")
        print()
        print("Worksheets:")
        for ws in sp.worksheets():
            print(f"  - {ws.title} ({ws.row_count} rows x {ws.col_count} cols)")
        print()
        print("Setup complete! You can now run the app:")
        print("  streamlit run ppt_job_app.py")
    except Exception as e:
        print(f"\nERROR connecting: {e}")
        print("Make sure you shared the spreadsheet with the service account email above.")


if __name__ == "__main__":
    main()
