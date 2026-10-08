$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw 'ไม่พบ Python 3: ติดตั้ง Python 3.11+ แล้วลองใหม่' }
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw 'ไม่พบ Node.js/npm: ติดตั้ง Node.js 20.9+ แล้วลองใหม่' }
if (-not (Test-Path .venv\Scripts\python.exe)) { py -3 -m venv .venv }
if (-not (Test-Path node_modules)) { npm install }
\.venv\Scripts\python.exe -m pip install -r requirements.txt
$backend = Start-Process -FilePath (Resolve-Path '.venv\Scripts\python.exe') -ArgumentList 'backend\server.py' -PassThru -WindowStyle Hidden
try {
  npm run dev
} finally {
  if ($backend -and -not $backend.HasExited) { Stop-Process -Id $backend.Id }
}
