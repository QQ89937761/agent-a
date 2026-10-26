param(
    [string]$PythonVersion = "3.13.15",
    [string]$NodeVersion = "24.21.0",
    [string]$PipVersion = "26.2"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

if (-not [Environment]::Is64BitOperatingSystem) {
    throw "Portable plugin runtimes require 64-bit Windows."
}

$repo = Split-Path -Parent $PSScriptRoot
$downloadRoot = Join-Path $repo "work/plugin-runtime-download"
$runtimeRoot = Join-Path $repo "work/plugin-runtime"
$pythonZip = Join-Path $downloadRoot "python-$PythonVersion-embed-amd64.zip"
$nodeZip = Join-Path $downloadRoot "node-v$NodeVersion-win-x64.zip"
$pipWheel = Join-Path $downloadRoot "pip-$PipVersion-py3-none-any.whl"
New-Item -ItemType Directory -Force -Path $downloadRoot | Out-Null
if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) {
    throw "curl.exe is required to download bundled runtimes."
}

function Get-Sha256Hex {
    param([string]$Path)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $stream = [IO.File]::OpenRead($Path)
        try {
            return ([BitConverter]::ToString($sha256.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
        } finally {
            $stream.Dispose()
        }
    } finally {
        $sha256.Dispose()
    }
}

function Get-VerifiedDownload {
    param([string]$Uri, [string]$Path, [string]$ExpectedHash)
    if (Test-Path -LiteralPath $Path) {
        $existingHash = Get-Sha256Hex $Path
        if ($existingHash.Equals($ExpectedHash, [StringComparison]::OrdinalIgnoreCase)) {
            return
        }
    }
    $partialPath = "$Path.partial"
    $mirrors = @(
        $Uri,
        $Uri.Replace("https://www.python.org/ftp/python/", "https://cdn.npmmirror.com/binaries/python/").Replace(
            "https://nodejs.org/dist/", "https://cdn.npmmirror.com/binaries/node/")
    ) | Select-Object -Unique
    $downloaded = $false
    foreach ($downloadUri in $mirrors) {
        & curl.exe --fail --location --silent --show-error --continue-at - --max-time 600 --output $partialPath $downloadUri
        if ($LASTEXITCODE -eq 0) {
            $downloaded = $true
            break
        }
    }
    if (-not $downloaded) {
        throw "Download failed; retry to resume: $Uri"
    }
    $actual = Get-Sha256Hex $partialPath
    if ($actual -ne $ExpectedHash) {
        Remove-Item -LiteralPath $partialPath -Force
        throw "SHA-256 verification failed: $Uri"
    }
    Move-Item -LiteralPath $partialPath -Destination $Path -Force
}

$pythonSlug = "python-" + ($PythonVersion -replace '\.', '')
$pythonRelease = (Invoke-WebRequest -UseBasicParsing -Uri "https://www.python.org/downloads/release/$pythonSlug/" -TimeoutSec 60).Content
$pythonFile = "python-$PythonVersion-embed-amd64.zip"
$pythonRow = [regex]::Match(
    $pythonRelease,
    ('(?s)<tr>(?:(?!</tr>).)*?' + [regex]::Escape($pythonFile) + '(?:(?!</tr>).)*?</tr>')
)
if (-not $pythonRow.Success) { throw "Python release does not contain $pythonFile" }
$checksumHtml = [regex]::Match($pythonRow.Value, '(?s)<code class="checksum">(.*?)</code>').Groups[1].Value
$pythonHash = ($checksumHtml -replace '<[^>]+>', '') -replace '\s', ''
if ($pythonHash -notmatch '^[0-9a-f]{64}$') { throw "Could not read Python SHA-256." }
$pythonUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
Get-VerifiedDownload $pythonUrl $pythonZip $pythonHash

$nodeBase = "https://nodejs.org/dist/v$NodeVersion"
$nodeSums = (Invoke-WebRequest -UseBasicParsing -Uri "$nodeBase/SHASUMS256.txt" -TimeoutSec 60).Content
$nodeName = [IO.Path]::GetFileName($nodeZip)
$nodeLine = $nodeSums -split "`n" | Where-Object { $_ -match "^([0-9a-f]{64})\s+\*?$([regex]::Escape($nodeName))\s*$" } | Select-Object -First 1
if (-not $nodeLine) { throw "Node.js checksum manifest does not contain $nodeName" }
$nodeHash = [regex]::Match($nodeLine, "^[0-9a-f]{64}").Value
Get-VerifiedDownload "$nodeBase/$nodeName" $nodeZip $nodeHash

$pipRelease = Invoke-RestMethod -Uri "https://pypi.org/pypi/pip/$PipVersion/json" -TimeoutSec 60
$pipArtifact = $pipRelease.urls | Where-Object filename -eq "pip-$PipVersion-py3-none-any.whl" | Select-Object -First 1
if (-not $pipArtifact) { throw "PyPI does not contain a universal pip $PipVersion wheel." }
$pipHash = $pipArtifact.digests.sha256
$pipUrl = $pipArtifact.url
Get-VerifiedDownload $pipUrl $pipWheel $pipHash

$stage = Join-Path $repo "work/plugin-runtime-stage"
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null
try {
    $pythonDir = Join-Path $stage "python"
    $nodeDir = Join-Path $stage "node"
    $sitePackages = Join-Path $pythonDir "Lib/site-packages"
    New-Item -ItemType Directory -Force -Path $pythonDir, $nodeDir, $sitePackages | Out-Null
    Expand-Archive -LiteralPath $pythonZip -DestinationPath $pythonDir -Force
    $hostVenv = & python.exe -I -c "import os, sysconfig; print(os.path.join(sysconfig.get_path('stdlib'), 'venv'))"
    if ($LASTEXITCODE -ne 0 -or -not $hostVenv) {
        throw "无法定位构建机 Python 标准库。"
    }
    if (-not (Test-Path -LiteralPath $hostVenv)) {
        throw "构建机 Python 缺少标准库 venv 模块。"
    }
    Copy-Item -LiteralPath $hostVenv -Destination (Join-Path $pythonDir "venv") -Recurse -Force
    Expand-Archive -LiteralPath $nodeZip -DestinationPath $nodeDir -Force
    $nodeNested = Join-Path $nodeDir "node-v$NodeVersion-win-x64"
    if (Test-Path -LiteralPath (Join-Path $nodeNested "node.exe")) {
        Get-ChildItem -LiteralPath $nodeNested -Force | Move-Item -Destination $nodeDir -Force
        Remove-Item -LiteralPath $nodeNested -Recurse -Force
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($pipWheel, $sitePackages)

    # This release is Windows x64 only. Keep x86/x64 pip launchers, not ARM64 shims.
    foreach ($name in @("t64-arm.exe", "w64-arm.exe")) {
        $armShim = [IO.Path]::GetFullPath((Join-Path $sitePackages "pip/_vendor/distlib/$name"))
        $stageRoot = [IO.Path]::GetFullPath($stage).TrimEnd('\') + '\'
        if (-not $armShim.StartsWith($stageRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Runtime shim path escapes staging directory."
        }
        if (Test-Path -LiteralPath $armShim) { Remove-Item -LiteralPath $armShim -Force }
    }

    $pth = Get-ChildItem -LiteralPath $pythonDir -Filter "python*._pth" | Select-Object -First 1
    if (-not $pth) { throw "Python embeddable 包缺少 python._pth。" }
    $stdlibZip = Get-Content -LiteralPath $pth.FullName | Where-Object { $_ -match '^python\d+\.zip$' } | Select-Object -First 1
    if (-not $stdlibZip) { throw "Python embeddable 包缺少标准库 ZIP 配置。" }
    @($stdlibZip, ".", "Lib\site-packages", "import site") |
        Set-Content -LiteralPath $pth.FullName -Encoding Ascii

    $python = Join-Path $pythonDir "python.exe"
    $node = Join-Path $nodeDir "node.exe"
    $npm = Join-Path $nodeDir "npm.cmd"
    if (-not (Test-Path $python) -or -not (Test-Path $node) -or -not (Test-Path $npm)) {
        throw "运行时包缺少 python.exe、node.exe 或 npm.cmd。"
    }
    & $python -c "import pip, venv; print('Python', __import__('sys').version.split()[0], 'pip', pip.__version__)"
    if ($LASTEXITCODE -ne 0) { throw "内置 Python/pip 冒烟测试失败。" }
    $venvProbe = Join-Path $stage "venv-probe"
    & $python -m venv --without-pip $venvProbe
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $venvProbe "Scripts/python.exe"))) {
        throw "内置 Python 无法创建插件隔离环境。"
    }
    Remove-Item -LiteralPath $venvProbe -Recurse -Force
    & $node --version
    if ($LASTEXITCODE -ne 0) { throw "内置 Node.js 冒烟测试失败。" }
    & $npm --version
    if ($LASTEXITCODE -ne 0) { throw "内置 npm 冒烟测试失败。" }

    $metadata = @{ python = $PythonVersion; node = $NodeVersion; pip = $PipVersion } | ConvertTo-Json -Compress
    [IO.File]::WriteAllText(
        (Join-Path $stage "runtime.json"), $metadata, [Text.UTF8Encoding]::new($false)
    )

    if (Test-Path -LiteralPath $runtimeRoot) {
        Remove-Item -LiteralPath $runtimeRoot -Recurse -Force
    }
    Move-Item -LiteralPath $stage -Destination $runtimeRoot
    Write-Host "Plugin runtimes ready: Python $PythonVersion / pip $PipVersion / Node.js $NodeVersion"
} catch {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
    throw
}
