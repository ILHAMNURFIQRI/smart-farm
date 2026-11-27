@echo off
title Smart Farm Server
echo ===================================================
echo Memulai Server Smart Farm (Backend + Dashboard)...
echo Dashboard dapat diakses di: http://localhost:5000
echo ===================================================
cd /d "%~dp0backend"
call .venv\Scripts\activate.bat
python app.py
pause
