@echo off
cd /d "%~dp0"
echo Starting PMUS Trading HUD...
.\venv\Scripts\streamlit.exe run pmus/dashboard.py --server.headless false --server.port 8501 %*

