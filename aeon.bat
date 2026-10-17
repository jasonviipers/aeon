@echo off
rem Launch AEON from the project folder using its own virtual environment.
rem   aeon.bat                      terminal UI (paper mode unless config.toml says otherwise)
rem   aeon.bat --check              verify config, keys and connectivity
rem   aeon.bat --brain heuristic    free dry run with the rule-based bot (no LLM key needed)
rem   aeon.bat --headless           no UI; log to the console (for a server)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run: creating the virtual environment...
  python -m venv .venv || exit /b 1
  ".venv\Scripts\python.exe" -m pip install --quiet -e . || exit /b 1
)
".venv\Scripts\python.exe" -m aeon %*
