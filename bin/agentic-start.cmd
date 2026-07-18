@echo off
rem Start-menu / shortcut-friendly launcher: starts the Agentic OS local
rem service and opens the dashboard. Safe to run when already running.
py "%~dp0..\.agentic\run" start
if errorlevel 1 pause
