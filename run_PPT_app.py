import streamlit.web.bootstrap
import sys
import webbrowser
import threading
import time

def open_browser():
    time.sleep(3)  # give Streamlit time to start
    webbrowser.open("http://127.0.0.1:8501")

if __name__ == "__main__":
    threading.Timer(1, open_browser).start()
    sys.argv = ["streamlit", "run", "ppt_job_app.py", "--server.headless=true", "--server.port=8501"]
    streamlit.web.bootstrap.run() # This is the line that starts the Streamlit server