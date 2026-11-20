<#
.SYNOPSIS
    Build a portable Windows ZIP with an embedded Python runtime.

.DESCRIPTION
    End users should not need Python, pip or a compiler.  This script downloads
    the official embeddable CPython distribution, installs the runtime
    dependencies into it, copies the source tree and writes
    dist/greenbubbles-win-<version>-portable.zip.

    The result is unsigned.  Sign it before publishing if you have a code
    signing certificate.
#>

param(
    [string]$PythonVersion = '3.12.10',
    [string]$PythonExe = 'python',
    [string]$OutDir = (Join-Path (Split-Path -Parent $PSScriptRoot) 'dist')
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Version = (Select-String -LiteralPath (Join-Path $Root 'pyproject.toml') -Pattern '^version\s*=\s*"([^"]+)"' |
    Select-Object -First 1).Matches[0].Groups[1].Value
if (-not $Version) { throw 'cannot read version from pyproject.toml' }

$Build = Join-Path $OutDir "build-portable-$Version"
$Cache = Join-Path $OutDir 'cache'
$Payload = Join-Path $Build 'greenbubbles-win'
$SitePackages = Join-Path $Payload 'python\Lib\site-packages'
$EmbedZip = Join-Path $Cache "python-$PythonVersion-embed-amd64.zip"

if (Test-Path -LiteralPath $Build) { Remove-Item -LiteralPath $Build -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Cache, $Payload, $SitePackages | Out-Null

if (-not (Test-Path -LiteralPath $EmbedZip)) {
    $url = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
    Write-Output "downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $EmbedZip -UseBasicParsing
}

Write-Output 'extracting embedded Python'
Expand-Archive -LiteralPath $EmbedZip -DestinationPath (Join-Path $Payload 'python') -Force

$pth = Get-ChildItem -LiteralPath (Join-Path $Payload 'python') -Filter 'python*._pth' | Select-Object -First 1
if (-not $pth) { throw 'python*._pth not found in the embedded distribution' }
$lines = Get-Content -LiteralPath $pth.FullName
$lines = $lines | ForEach-Object { $_ -replace '^#\s*import site$', 'import site' }
if ($lines -notcontains 'Lib\site-packages') { $lines += 'Lib\site-packages' }
# `.` in a ._pth file is the interpreter directory, so add the payload root
# explicitly; that lets `python -m unittest tests...` work from the Zip root.
if ($lines -notcontains '..') { $lines += '..' }
if ($lines -notcontains '..\src') { $lines += '..\src' }
[IO.File]::WriteAllLines($pth.FullName, $lines, (New-Object System.Text.UTF8Encoding($false)))

$wheels = Join-Path $Build 'wheels'
New-Item -ItemType Directory -Force -Path $wheels | Out-Null
$Requirements = Join-Path $Root 'requirements.lock'
if (-not (Test-Path -LiteralPath $Requirements)) { $Requirements = Join-Path $Root 'requirements.txt' }
Write-Output 'downloading runtime wheels for cp312/win_amd64'
& $PythonExe -m pip download -r $Requirements --dest $wheels `
    --only-binary=:all: --platform win_amd64 --python-version 3.12 `
    --implementation cp --abi cp312 --quiet
if ($LASTEXITCODE -ne 0) { throw 'pip download failed' }

Add-Type -AssemblyName System.IO.Compression.FileSystem
Get-ChildItem -LiteralPath $wheels -Filter '*.whl' | ForEach-Object {
    Write-Output "installing $($_.Name)"
    [IO.Compression.ZipFile]::ExtractToDirectory($_.FullName, $SitePackages)
}

$files = @(
    'README.md', 'README.zh-CN.md', 'LICENSE', 'NOTICE.md', 'THIRD_PARTY_NOTICES.md', 'PRIVACY.md',
    'SECURITY.md', 'CONTRIBUTING.md', 'CODE_OF_CONDUCT.md', 'CHANGELOG.md',
    'requirements.txt', 'pyproject.toml', 'greenbubbles.py',
    'greenbubbles.cmd', 'start-connector.cmd', 'stop-connector.cmd',
    'start-connector-bg.cmd', 'start-connector-local.cmd',
    'stop-connector-local.cmd', 'connector-status.cmd'
)
foreach ($name in $files) { Copy-Item -LiteralPath (Join-Path $Root $name) -Destination $Payload -Force }
foreach ($dir in @('src', 'docs', 'assets', 'skills', 'tests', 'scripts')) {
    Copy-Item -LiteralPath (Join-Path $Root $dir) -Destination $Payload -Recurse -Force
}
Remove-Item -LiteralPath (Join-Path $Payload 'scripts\diagnostics') -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem -LiteralPath $Payload -Recurse -Directory -Filter '__pycache__' |
    Remove-Item -Recurse -Force

$zip = Join-Path $OutDir "greenbubbles-win-$Version-portable.zip"
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
Compress-Archive -LiteralPath $Payload -DestinationPath $zip -CompressionLevel Optimal
$hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash
[IO.File]::WriteAllText("$zip.sha256", "$hash  $(Split-Path -Leaf $zip)`n", (New-Object System.Text.UTF8Encoding($false)))

Write-Output "built $zip"
Write-Output "sha256 $hash"
