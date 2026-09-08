# Show Seam to somebody, from this machine, without any hosting account.
#
#     .\demo.ps1
#
# Starts Seam configured for a public tunnel and prints what to do next. Meant
# for putting the product in front of two or three real businesses to find out
# whether it is worth going further - not for running a company on.
#
# What this is not: the database lives on this machine, the address disappears
# when you close the window, and mail is off unless you have configured it, so
# nobody can recover a forgotten password. For anything beyond showing it,
# read DEPLOY.md.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "No virtual environment. Creating one." -ForegroundColor Yellow
    python -m venv .venv
    & $py -m pip install -q --upgrade pip
    & $py -m pip install -q -r requirements.txt
}

# The tunnel reaches Seam over loopback and terminates TLS at its own end, so
# it is the only thing that can connect and may be believed about whether the
# outside connection was encrypted. Without this the session cookie is not
# marked Secure and anyone on the path could take a session.
$env:SEAM_HOST = "127.0.0.1"
$env:PORT = "5000"
$env:SEAM_TRUSTED_PROXY = "*"

# A demonstration should not be sending real invoices or chasing real people.
$env:SEAM_AGREEMENT_SCAN = "off"

Write-Host ""
Write-Host "Starting Seam on http://127.0.0.1:5000" -ForegroundColor Cyan
$app = Start-Process -FilePath $py -ArgumentList "app.py" -PassThru -NoNewWindow

Start-Sleep -Seconds 6
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:5000/healthz" -TimeoutSec 5
    if (-not $health.ok) { throw "healthz did not answer ok" }
} catch {
    Write-Host "Seam did not come up. Stopping." -ForegroundColor Red
    if ($app -and -not $app.HasExited) { Stop-Process -Id $app.Id -Force }
    exit 1
}
Write-Host "  it answers." -ForegroundColor Green

Write-Host ""
Write-Host "Sign in with any of these:" -ForegroundColor Cyan
Write-Host "  ops@seam.demo     demo1234    the company handing out work"
Write-Host "  build@seam.demo   demo1234    a contractor's side of the same records"
Write-Host "  supply@seam.demo  demo1234    a supplier"
Write-Host ""
Write-Host "Two accounts in two browsers shows the point of the product:"
Write-Host "one record, both sides, every change dated and attributed."
Write-Host ""

if (Get-Command cloudflared -ErrorAction SilentlyContinue) {
    Write-Host "Opening a public address..." -ForegroundColor Cyan
    Write-Host "Leave this window open. Closing it takes the address down."
    Write-Host ""
    cloudflared tunnel --url http://localhost:5000
} else {
    Write-Host "To show it to somebody who is not at this machine, install the" -ForegroundColor Yellow
    Write-Host "tunnel once:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "    winget install --id Cloudflare.cloudflared" -ForegroundColor White
    Write-Host ""
    Write-Host "then run this script again. It will print an https address that"
    Write-Host "works from anywhere for as long as this window stays open."
    Write-Host ""
    Write-Host "For now it is running at http://127.0.0.1:5000 - press Ctrl+C to stop."
    Write-Host ""
    try { Wait-Process -Id $app.Id } finally { }
}

if ($app -and -not $app.HasExited) { Stop-Process -Id $app.Id -Force }
