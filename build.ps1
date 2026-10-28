# Сборка LotusLanternPC.exe (один файл, без консоли). Запуск: powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path .venv)) { py -3 -m venv .venv }
& .venv\Scripts\python.exe -m pip install -r requirements.txt
& .venv\Scripts\pyinstaller.exe --noconfirm --onefile --windowed --name LotusLanternPC `
    --icon "$PSScriptRoot\assets\app.ico" --add-data "$PSScriptRoot\ui;ui" `
    --hidden-import pystray._win32 --hidden-import webview.platforms.winforms --hidden-import webview.platforms.edgechromium `
    --collect-all webview --workpath build --specpath build --distpath . lotus_lantern_pc.py
