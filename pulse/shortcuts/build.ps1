param(
  [string]$CherriVersion = "v2.2.0"
)

$ErrorActionPreference = "Stop"
$shortcutsRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent (Split-Path -Parent $shortcutsRoot)
$buildRoot = Join-Path $shortcutsRoot "build"
$toolRoot = Join-Path $env:TEMP "pulse-cherri-$CherriVersion"
$binary = Join-Path $toolRoot "cherri"
$asset = "cherri_linux-x86_64.zip"
$releaseUrl = "https://github.com/electrikmilk/cherri/releases/download/$CherriVersion/$asset"

if (-not (Test-Path $binary)) {
  New-Item -ItemType Directory -Force -Path $toolRoot | Out-Null
  $zip = Join-Path $toolRoot $asset
  Invoke-WebRequest -Uri $releaseUrl -OutFile $zip
  Expand-Archive -LiteralPath $zip -DestinationPath $toolRoot -Force
}

New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null
$repoPath = $repoRoot.Replace('\', '/')
$binaryPath = $binary.Replace('\', '/')
$buildPath = $buildRoot.Replace('\', '/')
$repoWsl = "/mnt/" + $repoPath.Substring(0, 1).ToLower() + $repoPath.Substring(2)
$binaryWsl = "/mnt/" + $binaryPath.Substring(0, 1).ToLower() + $binaryPath.Substring(2)
$buildWsl = "/mnt/" + $buildPath.Substring(0, 1).ToLower() + $buildPath.Substring(2)

foreach ($name in @("pulse-phone-context", "pulse-action-runner")) {
  $output = Join-Path $buildRoot "$name.shortcut"
  if (Test-Path $output) {
    Move-Item -LiteralPath $output -Destination (Join-Path $env:TEMP "$name.previous.shortcut") -Force
  }
  & wsl.exe $binaryWsl "$repoWsl/pulse/shortcuts/$name.cherri" --hubsign --no-ansi --derive-uuids "--output=$buildWsl/$name.shortcut"
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $output)) { throw "Cherri failed for $name" }
  if ((Get-Item $output).Length -lt 1024) { throw "Cherri output is unexpectedly small for $name" }
  $kind = (& wsl.exe file "$buildWsl/$name.shortcut" | Out-String).Trim()
  if ($kind -notmatch "Apple|property list|data|binary") { throw "Output does not look like a Shortcut artifact: $kind" }
  Write-Output "${name}: $kind"
}
