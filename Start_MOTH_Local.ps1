$Root = "C:\MOTH\app\moth_pi_setup"

$MainCmd = "cd `"$Root`"; .\.venv\Scripts\Activate.ps1; python -m uvicorn moth_analysis.api:app --host 127.0.0.1 --port 8000"
$CompanionCmd = "cd `"$Root`"; .\.venv\Scripts\Activate.ps1; python -m uvicorn moth_uas_analysis.companion_api:app --host 127.0.0.1 --port 8010"

Start-Process powershell.exe -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $MainCmd
Start-Sleep -Seconds 3

Start-Process powershell.exe -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $CompanionCmd
Start-Sleep -Seconds 4

Start-Process "http://127.0.0.1:8000/?v=local"
Start-Process "http://127.0.0.1:8000/static/launch_analysis.html?v=local"
