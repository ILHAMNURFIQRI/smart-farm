Write-Host "===================================================" -ForegroundColor Cyan
Write-Host "Memulai Server Smart Farm (Backend + Dashboard)..." -ForegroundColor Green
Write-Host "Dashboard dapat diakses di: http://localhost:5000" -ForegroundColor Yellow
Write-Host "===================================================" -ForegroundColor Cyan

Set-Location "$PSScriptRoot\backend"
& ".\.venv\Scripts\python.exe" app.py
