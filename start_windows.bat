@echo off
chcp 65001 >nul
cd /d "%~dp0"
py start_all.py
if errorlevel 1 python start_all.py
if errorlevel 1 pause
