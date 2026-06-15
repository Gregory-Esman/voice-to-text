@echo off
REM Launches the Windows (Groq online) build with no console window.
REM Uses the project's own virtualenv interpreter (Python isn't on PATH).
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0windows\app.py"
