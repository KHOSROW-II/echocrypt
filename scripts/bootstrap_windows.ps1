param(
    [ValidateSet("full", "standby", "manual", "rootless")]
    [string]$Mode = ""
)
$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))
if ([string]::IsNullOrWhiteSpace($Mode)) {
    & python echocrypt.py --bootstrap
} else {
    & python echocrypt.py --bootstrap $Mode
}
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
