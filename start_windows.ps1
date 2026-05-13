$ErrorActionPreference = "Stop"

python -m pip show fastapi *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Missing Python packages. Please run:"
  Write-Host "python -m pip install -r requirements.txt"
  exit 1
}

python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
