# 投石 · Toushi Web Frontend Startup Script (PowerShell version)

Write-Host "[INFO] 投石 · Toushi Web Frontend Startup Script" -ForegroundColor Green

# Set sandbox disable for Boss ZhiPin (Windows usually needs this)
$env:BOSS_NO_SANDBOX = "1"
Write-Host "[INFO] Detected Windows environment, set BOSS_NO_SANDBOX=1" -ForegroundColor Green

# Check .env file
if (-not (Test-Path .env)) {
    Write-Host "[WARN] .env file not found, please copy .env.example to .env and fill required config" -ForegroundColor Yellow
    Write-Host "[WARN] For example: copy .env.example .env then edit .env to fill LLM_API_KEY etc" -ForegroundColor Yellow
} else {
    # Check if LLM_API_KEY is set
    if (Select-String -Path .env -Pattern "LLM_API_KEY" -SimpleMatch) {
        $line = Select-String -Path .env -Pattern "LLM_API_KEY" | Select-Object -First 1
        $value = $line.Line.Split('=')[1].Trim()
        if (-not $value) {
            Write-Host "[WARN] .env LLM_API_KEY value is empty" -ForegroundColor Yellow
            Write-Host "[WARN] If you select Boss ZhiPin platform, this will cause failure" -ForegroundColor Yellow
        }
    } else {
        Write-Host "[WARN] .env does not contain LLM_API_KEY" -ForegroundColor Yellow
        Write-Host "[WARN] If you select Boss ZhiPin platform, this will cause failure" -ForegroundColor Yellow
        Write-Host "[WARN] ZhiLian platform does not require LLM API Key" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "[INFO] Starting 投石 · Toushi Web Frontend..." -ForegroundColor Green
Write-Host "[INFO] Please access http://localhost:5000 in your browser to use the interface" -ForegroundColor Green
Write-Host "[INFO] Please fill in API Key etc on the config page and save" -ForegroundColor Green
Write-Host "[INFO] Then switch to the run page, enter username and click start" -ForegroundColor Green
& uv run python -m toushi.web

Write-Host ""
Write-Host "[INFO] Web Frontend has stopped. If you encounter issues, please check the logs above or use the 'Ask AI for help' feature." -ForegroundColor Green