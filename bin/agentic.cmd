@echo off
rem Agentic OS command wrapper. Add this bin\ directory to PATH once, then
rem use `agentic <command>` from anywhere (e.g. `agentic project list`).
py "%~dp0..\.agentic\run" %*
