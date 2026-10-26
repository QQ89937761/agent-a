[CmdletBinding()]
param(
    [switch]$SkipDependencyInstall,
    [switch]$SkipUpdateSmoke,
    [switch]$SkipVersionIncrement
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Frontend = Join-Path $Repo "frontend"
$Artifact = Join-Path $Frontend "dist\XiaoA.exe"
$Unpacked = Join-Path $Frontend "dist\win-unpacked\resources"
$releaseDir = Join-Path $Repo "release-assets"
$Python = $env:XIAOA_BUILD_PYTHON
if (-not $Python) {
    $preferred = Join-Path $env:USERPROFILE "xiaoa-venv\Scripts\python.exe"
    $Python = if (Test-Path -LiteralPath $preferred) { $preferred } else { "python.exe" }
}
$Python = (Get-Command $Python -ErrorAction Stop).Source
# Tests, freezing and smoke checks must all use the same interpreter.
$env:XIAOA_BUILD_PYTHON = $Python
$env:PATH = (Split-Path -Parent $Python) + ";" + $env:PATH
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$File,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label,
        [string]$WorkingDirectory = $Repo
    )
    Write-Host "`n==> $Label" -ForegroundColor Cyan
    Push-Location $WorkingDirectory
    try {
        & $File @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "$Label 失败，退出码 $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

# Use .NET directly: inherited PSModulePath may hide Windows PowerShell cmdlets.
function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Path)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $stream = [IO.File]::OpenRead($Path)
        try { return ([BitConverter]::ToString($sha256.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
        finally { $stream.Dispose() }
    }
    finally { $sha256.Dispose() }
}

function Assert-RequiredCommand {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "缺少命令 $Name，请先安装并加入 PATH。"
    }
}

function Assert-WorkspacePath {
    param([string]$Path)
    $resolved = [IO.Path]::GetFullPath($Path)
    $root = $Repo.TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝操作工作区外路径: $resolved"
    }
}

function Update-ReleaseVersion {
    $packagePath = Join-Path $Frontend "package.json"
    $lockPath = Join-Path $Frontend "package-lock.json"
    $backendPath = Join-Path $Repo "backend\__init__.py"
    $current = [string](Get-Content -LiteralPath $packagePath -Raw | ConvertFrom-Json).version
    $match = [regex]::Match($current, '^(\d+)\.(\d+)\.(\d+)$')
    if (-not $match.Success) { throw "版本号不是标准三段式版本：$current" }
    # Windows PowerShell 5.1 cannot deserialize the lockfile's empty root key.
    $lockText = [IO.File]::ReadAllText($lockPath, [Text.Encoding]::UTF8)
    $lockTopMatch = [regex]::Match($lockText, '^\s*\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"version"\s*:\s*"([^"]+)"')
    $lockRootMatch = [regex]::Match($lockText, '"packages"\s*:\s*\{\s*""\s*:\s*\{(?s:.*?)"version"\s*:\s*"([^"]+)"')
    $backendText = [IO.File]::ReadAllText($backendPath, [Text.Encoding]::UTF8)
    $backendMatch = [regex]::Match($backendText, '__version__\s*=\s*["'']([^"'']+)["'']')
    if (-not $lockTopMatch.Success -or -not $lockRootMatch.Success -or
        $lockTopMatch.Groups[1].Value -ne $current -or $lockRootMatch.Groups[1].Value -ne $current -or
        -not $backendMatch.Success -or $backendMatch.Groups[1].Value -ne $current) {
        throw "前后端或 package-lock.json 版本不一致，已停止自动递增。"
    }
    $next = "{0}.{1}.{2}" -f $match.Groups[1].Value, $match.Groups[2].Value, ([int]$match.Groups[3].Value + 1)
    # npm updates both package.json and the lockfile without creating a Git tag.
    Invoke-Checked "npm.cmd" @("version", $next, "--no-git-tag-version", "--ignore-scripts") "版本自动递增：$current → $next" $Frontend | Out-Host
    $updated = $backendText.Remove($backendMatch.Groups[1].Index, $backendMatch.Groups[1].Length).Insert($backendMatch.Groups[1].Index, $next)
    [IO.File]::WriteAllText($backendPath, $updated, [Text.UTF8Encoding]::new($false))
    return $next
}

# A previous success manifest must not survive a failed new attempt.
$manifestPath = Join-Path $releaseDir "build-manifest.json"
if (Test-Path -LiteralPath $manifestPath) { Remove-Item -LiteralPath $manifestPath -Force }
# Portable UI and health checks share port 1985. Never test against an existing user's app.
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 1985)
try { $listener.Start() }
catch { throw "请先正常退出正在运行的小A后再打包验收（1985 端口被占用）；不会自动结束你的应用。" }
finally { $listener.Stop() }

Assert-RequiredCommand "git.exe"
Assert-RequiredCommand "node.exe"
Assert-RequiredCommand "npm.cmd"
Assert-RequiredCommand "python.exe"

# release_files.py 只允许 Git 已登记的应用资源进入云端 payload。
# 未登记的应用源码如果继续打包，会出现“本机有、正式包没有”的隐蔽缺陷；这里宁可停下来，也不产出不完整 EXE。
$trackedStatus = & git.exe -C $Repo status --porcelain=v1 --untracked-files=all -- backend deploy frontend/src config.example.toml
if ($LASTEXITCODE -ne 0) { throw "读取 Git 工作区状态失败。" }
$untrackedApplicationFiles = @(
    $trackedStatus | Where-Object {
        $_ -match '^\?\? ' -and
        $_ -notmatch '^\?\? (backend|frontend)/test_[^/]+$' -and
        $_ -notmatch '^\?\? backend/.*/test_[^/]+$'
    }
)
if ($untrackedApplicationFiles.Count -gt 0) {
    throw ("发现未纳入 Git 的应用源码，已停止打包以防功能漏打：`n" +
        ($untrackedApplicationFiles -join "`n") +
        "`n请先将这些源码纳入 Git（不等于上传 GitHub），再重新双击打包。")
}

$version = if ($SkipVersionIncrement) {
    [string](Get-Content -LiteralPath (Join-Path $Frontend "package.json") -Raw | ConvertFrom-Json).version
} else {
    Update-ReleaseVersion
}
Write-Host "小A正式版一键打包，版本 $version" -ForegroundColor Green
Write-Host "Python: $Python"

if (-not $SkipDependencyInstall) {
    Invoke-Checked "npm.cmd" @("ci", "--no-audit", "--no-fund") "按 package-lock.json 安装前端依赖" $Frontend
    Invoke-Checked $Python @("-m", "pip", "install", "--disable-pip-version-check", "-r", "backend/requirements.txt", "-r", "backend/requirements-optional.txt", "-r", "backend/requirements-build.txt") "安装测试和构建依赖"
}

$distDirectory = [IO.Path]::GetFullPath((Join-Path $Frontend "dist"))
Assert-WorkspacePath $distDirectory
if (Test-Path -LiteralPath $distDirectory) {
    Remove-Item -LiteralPath $distDirectory -Recurse -Force
}

# dist 已经串起：前端检查、严格后端全量测试、冻结后端、插件运行时、Portable EXE 和冻结后端冒烟。
Invoke-Checked "npm.cmd" @("run", "dist") "执行完整构建流水线" $Frontend

$required = @(
    $Artifact,
    (Join-Path $Unpacked "app.asar"),
    (Join-Path $Unpacked "backend\xiaoa-backend.exe"),
    (Join-Path $Unpacked "plugin-runtime\runtime.json"),
    (Join-Path $Unpacked "plugin-runtime\python\python.exe"),
    (Join-Path $Unpacked "plugin-runtime\node\node.exe")
)
$missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
if ($missing.Count -gt 0) {
    throw ("打包完成但产物完整性校验失败，缺少：`n" + ($missing -join "`n"))
}

$artifactInfo = Get-Item -LiteralPath $Artifact
if ($artifactInfo.Length -lt 50MB) {
    throw "EXE 体积异常偏小($($artifactInfo.Length) bytes)，拒绝作为正式包。"
}

Invoke-Checked $Python @("deploy\verify_release.py") "逐文件核对源码、云端 payload、ASAR、冻结模块和运行环境"

if (-not $SkipUpdateSmoke) {
    Invoke-Checked $Python @("deploy\smoke_portable_update.py", "frontend\dist\XiaoA.exe") "验证 Portable 更新包可替换与重启" $Repo
}

$hash = Get-Sha256Hex $Artifact
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
$verified = -not $SkipUpdateSmoke
$manifest = [ordered]@{
    version = $version
    artifact = "XiaoA.exe"
    size_bytes = $artifactInfo.Length
    sha256 = $hash
    built_at = (Get-Date).ToUniversalTime().ToString("o")
    verified = $verified
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $releaseDir "build-manifest.json") -Encoding utf8

if ($verified) {
    Write-Host "`n打包成功，已通过完整性和 Portable 更新冒烟检查。" -ForegroundColor Green
} else {
    Write-Warning "已生成 EXE，但跳过了更新冒烟：清单标记 verified=false，不可视为完整验收。"
}
Write-Host "EXE: $Artifact"
Write-Host "SHA256: $hash"
Write-Host "清单: $(Join-Path $releaseDir 'build-manifest.json')"
