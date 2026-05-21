$ErrorActionPreference = "Stop"

if (Test-Path ".env") {
    Get-Content ".env" | Where-Object { $_ -notmatch '^\s*#' -and $_ -match '=' } | ForEach-Object {
        $key, $val = $_ -split '=', 2
        [System.Environment]::SetEnvironmentVariable($key.Trim(), $val.Trim(), "Process")
    }
}

python -m pip show fastapi *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Missing Python packages. Please run:"
  Write-Host "python -m pip install -r requirements.txt"
  exit 1
}

python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
