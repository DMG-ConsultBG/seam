# Put Seam on GitHub, in one command.
#
#     .\publish.ps1                 # private repository (default)
#     .\publish.ps1 -Public         # public repository
#
# Everything except the account itself is done here: the repository is created,
# the remote is set, the branch is pushed, and the address is printed.
#
# Two things must exist first, and neither can be done from a script:
#
#     1. a GitHub account          https://github.com/signup
#     2. winget install --id GitHub.cli
#        gh auth login             (choose HTTPS, log in through the browser)
#
# Run this afterwards.

param([switch]$Public)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Host "The GitHub command line tool is not installed." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "    winget install --id GitHub.cli" -ForegroundColor White
    Write-Host ""
    Write-Host "Close and reopen this window afterwards, then:"
    Write-Host ""
    Write-Host "    gh auth login" -ForegroundColor White
    Write-Host ""
    Write-Host "and run this script again."
    exit 1
}

# `gh auth status` writes to stderr and returns non-zero when signed out, which
# is not an error worth stopping on - it is the thing being tested.
$ErrorActionPreference = "Continue"
gh auth status 2>&1 | Out-Null
$signedIn = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = "Stop"
if (-not $signedIn) {
    Write-Host "Not signed in to GitHub. Run:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "    gh auth login" -ForegroundColor White
    Write-Host ""
    exit 1
}

$visibility = if ($Public) { "--public" } else { "--private" }
$word = if ($Public) { "public" } else { "private" }

Write-Host ""
Write-Host "Creating a $word repository named 'seam'..." -ForegroundColor Cyan

# A last look before anything leaves the machine. .gitignore already excludes
# the keys, the database and the uploads; this is the check that it worked,
# because git remembers a key pushed once even after it is deleted.
$leaks = git ls-files | Select-String -Pattern '^\.secret$|^\.data_key$|^\.vapid_keys$|^\.ref_keys$|^seam\.db$|^seam\.env$|^backups/|^uploads/'
if ($leaks) {
    Write-Host "Stopping: these would be published and must not be." -ForegroundColor Red
    $leaks | ForEach-Object { Write-Host "    $_" }
    exit 1
}
Write-Host "  no keys, database or uploads are tracked." -ForegroundColor Green

gh repo create seam $visibility --source=. --remote=origin --push

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "That did not work. If the name is taken, pick another:" -ForegroundColor Yellow
    Write-Host "    gh repo create seam-platform $visibility --source=. --remote=origin --push"
    exit 1
}

$url = (gh repo view --json url -q .url)
Write-Host ""
Write-Host "Done. Seam is at:" -ForegroundColor Green
Write-Host "    $url" -ForegroundColor White
Write-Host ""
if (-not $Public) {
    Write-Host "It is private. The listing form cannot read it until you either"
    Write-Host "make it public or add the buyer as a collaborator:"
    Write-Host "    gh repo edit --visibility public --accept-visibility-change-consequences"
    Write-Host ""
}
Write-Host "Paste that address into the listing form's GitHub option."
