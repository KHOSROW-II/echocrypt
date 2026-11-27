param(
 [Parameter(Mandatory=$true)][ValidateSet("chrome","edge","firefox")][string]$Browser,
 [Parameter(Mandatory=$true)][string]$ExtensionId,
 [Parameter(Mandatory=$true)][string]$NativeHostPath
)
$ErrorActionPreference="Stop"
if ($Browser -eq "firefox") {
 if ($ExtensionId -ne "echocrypt@khosrow-ii.local") { throw "Unexpected Firefox add-on ID." }
 $hostDir=Join-Path $env:APPDATA "Mozilla\NativeMessagingHosts"
} else {
 if ($ExtensionId -notmatch '^[a-p]{32}$') { throw "Chromium extension ID must be 32 characters in a-p." }
 if ($Browser -eq "chrome") { $hostDir=Join-Path $env:LOCALAPPDATA "Google\Chrome\User Data\NativeMessagingHosts" }
 else { $hostDir=Join-Path $env:LOCALAPPDATA "Microsoft\Edge\User Data\NativeMessagingHosts" }
}
if (!(Test-Path $NativeHostPath -PathType Leaf)) { throw "Native host executable not found: $NativeHostPath" }
New-Item -ItemType Directory -Force -Path $hostDir | Out-Null
$manifest=@{name="org.echocrypt.tab_audio";description="ECHOCRYPT local tab audio bridge";path=(Resolve-Path $NativeHostPath).Path;type="stdio"}
if ($Browser -eq "firefox") { $manifest.allowed_extensions=@($ExtensionId) }
else { $manifest.allowed_origins=@("chrome-extension://$ExtensionId/") }
$manifestPath=Join-Path $hostDir "org.echocrypt.tab_audio.json"
$manifest | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 $manifestPath
$configDir=Join-Path $env:APPDATA "ECHOCRYPT"
New-Item -ItemType Directory -Force -Path $configDir | Out-Null
$configPath=Join-Path $configDir "browser-native-host.json"
$config=@{}
if (Test-Path $configPath) { try { $config=Get-Content $configPath -Raw | ConvertFrom-Json -AsHashtable } catch { $config=@{} } }
if (!$config.ContainsKey("extensions")) { $config.extensions=@{} }
$config.extensions[$Browser]=$ExtensionId
$config | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 $configPath
Write-Host "ECHOCRYPT Native Messaging host registered for $Browser."
