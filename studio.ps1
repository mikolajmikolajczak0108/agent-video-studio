$ErrorActionPreference = 'Stop'
$studioPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $studioPython)) {
    throw 'Brak srodowiska. Uruchom: python -m venv .venv; .venv\Scripts\python -m pip install -r requirements.txt'
}
& $studioPython (Join-Path $PSScriptRoot 'scripts\studio.py') @args
exit $LASTEXITCODE
