@echo off
rem Start the Tacx NEO launcher without a console window (double-click, or point a desktop shortcut here).
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0tacx_neo.py" launcher
