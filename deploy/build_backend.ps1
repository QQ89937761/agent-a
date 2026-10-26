param(
    [string]$Python = $env:XIAOA_BUILD_PYTHON
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if (-not $Python) {
    $PreferredPython = Join-Path $env:USERPROFILE "xiaoa-venv\Scripts\python.exe"
    $Python = if (Test-Path -LiteralPath $PreferredPython) { $PreferredPython } else { "python" }
}

foreach ($Requirements in @(
    "backend\requirements.txt",
    "backend\requirements-optional.txt",
    "backend\requirements-build.txt"
)) {
    & $Python -m pip install --disable-pip-version-check -r (Join-Path $ProjectRoot $Requirements)
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed: $Requirements" }
}

# Resolve once before narrowing PATH: foreign tool-bundled DLLs must not win DLL lookup.
$Python = (Get-Command $Python -ErrorAction Stop).Source
$OriginalPath = $env:PATH
$GitCommand = Get-Command git.exe -ErrorAction Stop
$GitBin = Split-Path -Parent $GitCommand.Source
$PythonBase = & $Python -c "import sys; print(sys.base_prefix)"
if ($LASTEXITCODE -ne 0) { throw "Cannot locate the build interpreter." }
$env:PATH = @((Split-Path -Parent $Python), $PythonBase, $GitBin, "$env:SystemRoot\System32", $env:SystemRoot) -join ";"
Push-Location $ProjectRoot
try {
    & $Python -m PyInstaller --noconfirm --clean `
        --distpath (Join-Path $ProjectRoot "dist") `
        --workpath (Join-Path $ProjectRoot "work\pyinstaller") `
        (Join-Path $ProjectRoot "deploy\xiaoa_backend.spec")
    if ($LASTEXITCODE -ne 0) { throw "Backend packaging failed." }
}
finally {
    Pop-Location
    $env:PATH = $OriginalPath
}

$BackendExe = Join-Path $ProjectRoot "dist\xiaoa-backend\xiaoa-backend.exe"
if (-not (Test-Path -LiteralPath $BackendExe)) {
    throw "Backend executable was not created: $BackendExe"
}
$CertificateBundle = Join-Path $ProjectRoot "dist\xiaoa-backend\_internal\certifi\cacert.pem"
if (-not (Test-Path -LiteralPath $CertificateBundle)) {
    throw "Backend package is missing the TLS certificate bundle: $CertificateBundle"
}

# Exercise the frozen model APIs, including a missing PEM and rejected HTTPS cert.
& $Python (Join-Path $ProjectRoot "deploy\smoke_backend.py") $BackendExe --without-ca-bundle
if ($LASTEXITCODE -ne 0) { throw "Frozen backend failed model/TLS smoke tests." }

Write-Host "Backend package ready and model/TLS smoke passed: $BackendExe"
