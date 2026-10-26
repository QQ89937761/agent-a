"""GitHub Release metadata and verified asset downloads."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse
from pathlib import Path
from typing import Any

UPDATE_REPOSITORY = "QQ89937761/agent-a"
_REDIRECT_HOSTS = frozenset({
    "api.github.com", "github.com", "release-assets.githubusercontent.com",
    "objects.githubusercontent.com", "github-releases.githubusercontent.com",
})


def _update_api_url() -> str:
    repository = UPDATE_REPOSITORY.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        return ""
    return f"https://api.github.com/repos/{repository}/releases/latest"


def _same_update_origin(url: str) -> bool:
    repository = UPDATE_REPOSITORY.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        return False
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and port in (None, 443)
        and not parsed.username
        and not parsed.password
        and parsed.path.startswith(f"/{repository}/releases/")
        and not parsed.query
        and not parsed.fragment
    )


_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+][0-9A-Za-z.-]+)?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UPDATE_JOB_DEFAULTS = {
    "stage": "idle",
    "percent": 0,
    "message": "尚未开始更新",
    "error": "",
}


class UpdateCancelled(RuntimeError):
    """The user cancelled before replacement began."""


class ReleaseError(RuntimeError):
    """Raised when the signed-by-URL update metadata is unusable."""


def update_state_path(home: str | Path) -> Path:
    """Return the durable update state location for one XiaoA data home."""
    return Path(home) / "data" / "update-state.json"


def load_update_state(path: str | Path) -> dict[str, Any]:
    """Load a recoverable update state; corrupted state fails closed to idle."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(value, dict) and isinstance(value.get("stage"), str):
            return {**_UPDATE_JOB_DEFAULTS, **value}
    except (OSError, ValueError, TypeError):
        pass
    return dict(_UPDATE_JOB_DEFAULTS)


def save_update_state(path: str | Path, values: dict[str, Any]) -> None:
    """Atomically persist update state so a helper can outlive the backend."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, target)


def version_key(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(str(value or "").strip())
    if not match:
        raise ReleaseError("更新版本号无效")
    return tuple(int(part or 0) for part in match.groups())


def validate_manifest(value: Any) -> dict[str, Any] | None:
    """Validate GitHub's latest-release response and select its verified EXE."""
    if not isinstance(value, dict) or value.get("draft") or value.get("prerelease"):
        raise ReleaseError("GitHub Release 数据无效")
    repository = UPDATE_REPOSITORY.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ReleaseError("GitHub Releases 更新源尚未配置")
    tag = str(value.get("tag_name", "")).strip()
    version = tag.removeprefix("v")
    version_key(version)
    tag_path = quote(tag, safe="")
    release_url = str(value.get("html_url", "")).strip()
    expected_release_url = f"https://github.com/{repository}/releases/tag/{tag_path}"
    if release_url != expected_release_url:
        raise ReleaseError("GitHub Release 地址无效")
    assets = value.get("assets")
    if not isinstance(assets, list):
        raise ReleaseError("GitHub Release 附件列表无效")
    asset = next((item for item in assets if isinstance(item, dict) and item.get("name") == "XiaoA.exe"), None)
    if asset is None:
        raise ReleaseError("GitHub Release 缺少 XiaoA.exe")
    url = str(asset.get("browser_download_url", "")).strip()
    digest = str(asset.get("digest", "")).strip().lower().removeprefix("sha256:")
    expected_asset_url = f"https://github.com/{repository}/releases/download/{tag_path}/XiaoA.exe"
    if url != expected_asset_url or not _SHA256_RE.fullmatch(digest):
        raise ReleaseError("GitHub Release 安装包信息无效")
    return {
        "version": version,
        "tag": tag,
        "portable": {"name": "XiaoA.exe", "url": url, "sha256": digest},
        "release_url": release_url,
        "notes": str(value.get("body", ""))[:4000],
    }


class _NoUpdateRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        try:
            port = parsed.port
        except ValueError:
            port = -1
        if (parsed.scheme != "https" or parsed.hostname not in _REDIRECT_HOSTS
                or port not in (None, 443) or parsed.username or parsed.password):
            raise ReleaseError("GitHub 更新下载重定向目标不可信")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_update_url(url: str, timeout: float):
    parsed = urlparse(url)
    api_url = _update_api_url()
    api = urlparse(api_url) if api_url else None
    try:
        port = parsed.port
    except ValueError:
        port = -1
    is_api = bool(api and url == api_url and port in (None, 443)
                  and not parsed.username and not parsed.password
                  and not parsed.query and not parsed.fragment)
    if not (is_api or _same_update_origin(url)):
        raise ReleaseError("拒绝访问非官方 GitHub Release 地址")
    headers = {
        "Accept": "application/vnd.github+json, application/octet-stream",
        "User-Agent": "XiaoA-Updater/1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    request = urllib.request.Request(url, headers=headers)
    return urllib.request.build_opener(_NoUpdateRedirect()).open(request, timeout=timeout)


def fetch_manifest(timeout: float = 15) -> dict[str, Any] | None:
    url = _update_api_url()
    if not url:
        return None
    try:
        with _open_update_url(url, timeout) as response:
            return validate_manifest(json.load(response))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            exc.close()
            return None
        raise ReleaseError(f"无法读取 GitHub Releases：HTTP {exc.code}") from exc
    except ReleaseError:
        raise
    except Exception as exc:
        raise ReleaseError(f"无法读取 GitHub Releases：{exc}") from exc


def download_asset(asset: dict[str, str], directory: str | Path, timeout: float = 60, progress=None) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / Path(asset["name"]).name
    digest = hashlib.sha256()
    try:
        with _open_update_url(asset["url"], timeout) as response, target.open("wb") as output:
            total = int(response.headers.get("Content-Length") or 0)
            received = 0
            if progress:
                progress(0, received, total)
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
                received += len(chunk)
                if progress:
                    progress(min(99, round(received * 100 / total)) if total else 0, received, total)
    except UpdateCancelled:
        target.unlink(missing_ok=True)
        raise
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise ReleaseError(f"下载GitHub Release 安装包失败：{exc}") from exc
    if digest.hexdigest() != asset["sha256"]:
        target.unlink(missing_ok=True)
        raise ReleaseError("安装包 SHA-256 校验失败，已删除不可信文件")
    if progress:
        try:
            progress(100, received, total)
        except UpdateCancelled:
            target.unlink(missing_ok=True)
            raise
    return target


def portable_update_context(manifest: dict[str, Any], directory: str | Path,
                            progress=None) -> tuple[Path, Path]:
    '''Download the GitHub Release EXE and return (temp_dir, file).'''
    temp_dir = Path(directory)
    temp_dir.mkdir(parents=True, exist_ok=True)
    target = download_asset(manifest["portable"], temp_dir, progress=progress)
    return temp_dir, target


def prepare_portable_update(manifest: dict[str, Any], *, executable: str | Path,
                            parent_pid: int, backend_pid: int, progress=None,
                            state_path: str | Path | None = None,
                            log_path: str | Path | None = None,
                            health_url: str = "http://127.0.0.1:1985/health") -> dict[str, str]:
    '''Download and verify only; do not close the app until the user restarts.'''
    if os.name != "nt":
        raise ReleaseError("正式版自动更新目前仅支持 Windows")
    target = Path(executable).resolve()
    if target.suffix.lower() != ".exe" or not target.is_file():
        raise ReleaseError("找不到当前正式版 EXE，开发版不会自动替换源码目录")
    temp_dir = Path(tempfile.mkdtemp(prefix="xiaoa-update-"))
    try:
        _, downloaded = portable_update_context(manifest, temp_dir, progress=progress)
        helper = temp_dir / "install-update.ps1"
        backup = target.with_name(target.name + f".backup-{temp_dir.name}")
        helper.write_text(r'''
param([string]$ConfigPath)
$ErrorActionPreference = "Stop"

$config = Get-Content -Raw -Encoding UTF8 -LiteralPath $ConfigPath | ConvertFrom-Json
$ParentPid = [int]$config.ParentPid
$BackendPid = [int]$config.BackendPid
$Target = [string]$config.Target
$Downloaded = [string]$config.Downloaded
$Backup = [string]$config.Backup
$StatePath = [string]$config.StatePath
$LogPath = [string]$config.LogPath
$ExpectedVersion = [string]$config.ExpectedVersion
$ExpectedSha256 = [string]$config.ExpectedSha256
$HealthUrl = [string]$config.HealthUrl
$WorkDir = [string]$config.WorkDir
$RestartArgs = @()
if ($config.UserDataDir) { $RestartArgs = @("--user-data-dir=$([string]$config.UserDataDir)") }

function Write-Log([string]$Message) {
  try {
    $directory = Split-Path -Parent $LogPath
    if ($directory) { New-Item -ItemType Directory -Force -Path $directory | Out-Null }
    Add-Content -LiteralPath $LogPath -Value ("[{0}] {1}" -f (Get-Date).ToString("o"), $Message) -Encoding UTF8
  } catch {}
}

function Write-State([string]$Stage, [int]$Percent, [string]$Message, [string]$Error = "") {
  $value = @{
    stage = $Stage; percent = $Percent; message = $Message; error = $Error
    latest = $ExpectedVersion; updated_at = (Get-Date).ToUniversalTime().ToString("o")
  }
  try {
    $directory = Split-Path -Parent $StatePath
    if ($directory) { New-Item -ItemType Directory -Force -Path $directory | Out-Null }
    $temporary = "$StatePath.$PID.tmp"
    [IO.File]::WriteAllText($temporary, ($value | ConvertTo-Json -Compress), (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temporary -Destination $StatePath -Force
  } catch { Write-Log ("写入更新状态失败: " + $_.Exception.Message) }
  Write-Log ("$Stage $Message")
}

function Get-Sha256([string]$Path) {
  $algorithm = [System.Security.Cryptography.SHA256]::Create()
  $stream = [System.IO.File]::OpenRead($Path)
  try { return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace("-", "").ToLowerInvariant() }
  finally { $stream.Dispose(); $algorithm.Dispose() }
}

function Stop-IfAlive([int]$ProcessId) {
  if ($ProcessId -le 0) { return }
  try { Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue } catch {}
}

function Stop-OldAppTree([int]$ProcessId) {
  # Only processes captured BEFORE this updater was created may be stopped.
  # No taskkill /T: that would include the updater itself. Check start times
  # as well as PIDs so a reused PID cannot terminate an unrelated process.
  foreach ($item in $config.OldProcesses) {
    try {
      $old = Get-Process -Id ([int]$item.pid) -ErrorAction SilentlyContinue
      if (-not $old) { continue }
      $epoch = [DateTime]::SpecifyKind([DateTime]"1970-01-01", [DateTimeKind]::Utc)
      $started = ($old.StartTime.ToUniversalTime() - $epoch).TotalSeconds
      if ([Math]::Abs($started - [double]$item.created) -lt 0.1) {
        Stop-Process -Id $old.Id -Force -ErrorAction SilentlyContinue
      }
    } catch { Write-Log ("关闭旧进程失败: " + $_.Exception.Message) }
  }
}

function Restore-Backup {
  try {
    if (-not (Test-Path -LiteralPath $Target) -and (Test-Path -LiteralPath $Backup)) {
      Move-Item -LiteralPath $Backup -Destination $Target -Force
      return $true
    }
  } catch { Write-Log ("恢复旧 EXE 失败: " + $_.Exception.Message) }
  return (Test-Path -LiteralPath $Target)
}

[IO.File]::WriteAllText((Join-Path $WorkDir "worker-ready"), [string]$PID)
$go = Join-Path $WorkDir "restart-approved"
for ($i = 0; $i -lt 200 -and -not (Test-Path -LiteralPath $go); $i++) { Start-Sleep -Milliseconds 100 }
if (-not (Test-Path -LiteralPath $go)) { Write-Log "更新未获重启确认，停止"; exit 1 }
try {
Write-State "replacing" 100 "正在关闭旧版本并替换文件…"
Start-Sleep -Milliseconds 1000
Stop-OldAppTree $ParentPid
for ($i = 0; $i -lt 80; $i++) {
  $alive = $false
  foreach ($processId in @($ParentPid, $BackendPid)) {
    if ($processId -gt 0 -and (Get-Process -Id $processId -ErrorAction SilentlyContinue)) { $alive = $true }
  }
  if (-not $alive) { break }
  Start-Sleep -Milliseconds 250
}

$replaced = $false
$newTargetCreated = $false
$failure = "替换新 EXE 失败"
for ($i = 0; $i -lt 40 -and -not $replaced; $i++) {
  try {
    if ((Test-Path -LiteralPath $Target) -and -not (Test-Path -LiteralPath $Backup)) {
      Move-Item -LiteralPath $Target -Destination $Backup -Force
    }
    if (-not (Test-Path -LiteralPath $Target) -and (Test-Path -LiteralPath $Downloaded)) {
      $newTargetCreated = $true
      Copy-Item -LiteralPath $Downloaded -Destination $Target -Force
    }
    if (-not (Test-Path -LiteralPath $Target)) { throw "新 EXE 未落盘" }
    $actualSha256 = Get-Sha256 $Target
    if ($actualSha256 -ne $ExpectedSha256.ToLowerInvariant()) { throw "新 EXE SHA-256 校验失败" }
    $replaced = $true
  } catch {
    $failure = $_.Exception.Message
    if ($newTargetCreated) { Remove-Item -LiteralPath $Target -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500
  }
}
if (-not $replaced) {
  $restored = Restore-Backup
  Write-State "rolled_back" 0 "更新失败，已恢复旧版本。" $failure
  if ($restored) { Start-Process -FilePath $Target -WindowStyle Hidden -WorkingDirectory (Split-Path -Parent $Target) | Out-Null }
  exit 1
}

Write-State "restarting" 100 "文件替换完成，正在启动新版本…"
# Portable 外壳不应在升级重启时留下额外任务栏按钮；Electron 自己负责显示悬浮球。
if ($RestartArgs.Count) {
  $newProcess = Start-Process -FilePath $Target -WindowStyle Hidden -WorkingDirectory (Split-Path -Parent $Target) -ArgumentList $RestartArgs -PassThru
} else {
  $newProcess = Start-Process -FilePath $Target -WindowStyle Hidden -WorkingDirectory (Split-Path -Parent $Target) -PassThru
}
Write-State "verifying" 100 "正在验证新版本是否启动…"
$verified = $false
$startupDeadline = (Get-Date).AddSeconds(300)
while ((Get-Date) -lt $startupDeadline) {
  try {
    $request = [System.Net.HttpWebRequest]::Create($HealthUrl)
    $request.Timeout = 3000
    $request.ReadWriteTimeout = 3000
    $request.Proxy = $null
    $response = $request.GetResponse()
    $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
    try { $health = $reader.ReadToEnd() | ConvertFrom-Json }
    finally { $reader.Dispose(); $response.Dispose() }
    $actualVersion = ([string]$health.version).TrimStart([char[]]"vV")
    $expectedVersionNormalized = $ExpectedVersion.TrimStart([char[]]"vV")
    if ($health.ok -eq $true -and $actualVersion -eq $expectedVersionNormalized) {
      $verified = $true
      break
    }
  } catch {}
  Start-Sleep -Milliseconds 1000
}
if (-not $verified) {
  Write-Log "新版本健康检查超时，开始回滚"
  try { if ($newProcess -and -not $newProcess.HasExited) {
    & "$env:SystemRoot\System32\taskkill.exe" /PID $newProcess.Id /T /F | Out-Null
  } } catch {}
  if (Test-Path -LiteralPath $Target) { Remove-Item -LiteralPath $Target -Force -ErrorAction SilentlyContinue }
  $restored = Restore-Backup
  Write-State "rolled_back" 0 "新版本启动验证失败，已恢复旧版本。" "健康检查未返回 v$ExpectedVersion"
  if ($restored) { Start-Process -FilePath $Target -WindowStyle Hidden -WorkingDirectory (Split-Path -Parent $Target) | Out-Null }
  exit 1
}

Write-State "done" 100 "更新完成，当前运行 v$ExpectedVersion。"
Remove-Item -LiteralPath $Backup -Force -ErrorAction SilentlyContinue
Write-Log "更新成功，保留临时目录用于诊断: $WorkDir"
} catch {
  $failure = $_.Exception.Message
  Write-Log ("更新器异常: " + $failure)
  if ($newTargetCreated -and (Test-Path -LiteralPath $Target)) {
    Remove-Item -LiteralPath $Target -Force -ErrorAction SilentlyContinue
  }
  $restored = Restore-Backup
  if ($restored) {
    Write-State "rolled_back" 0 "更新异常，已恢复旧版本。" $failure
    try { Start-Process -FilePath $Target -WindowStyle Hidden -WorkingDirectory (Split-Path -Parent $Target) | Out-Null } catch {}
  } else {
    Write-State "error" 0 "更新异常，请手动恢复旧 EXE 备份。" $failure
  }
  exit 1
}
''', encoding="utf-8-sig")
        state_path = Path(state_path or (target.parent / "XiaoA-data" / "data" / "update-state.json")).resolve()
        log_path = Path(log_path or (temp_dir / "update.log")).resolve()
        asset = manifest.get("portable") or {}
        config = temp_dir / "update-config.json"
        config.write_text(json.dumps({
            "ParentPid": int(parent_pid),
            "BackendPid": int(backend_pid),
            "Target": str(target),
            "Downloaded": str(downloaded),
            "Backup": str(backup),
            "StatePath": str(state_path),
            "LogPath": str(log_path),
            "ExpectedVersion": str(manifest["version"]),
            "ExpectedSha256": str(asset.get("sha256", "")),
            "HealthUrl": health_url,
            "WorkDir": str(temp_dir),
            "UserDataDir": os.getenv("XIAOA_USER_DATA_DIR", ""),
        }), encoding="utf-8")
        return {"version": manifest["version"], "target": str(target), "prepared_config": str(config)}
    except Exception:
        import shutil
        resolved = temp_dir.resolve()
        if resolved.parent != Path(tempfile.gettempdir()).resolve() or not resolved.name.startswith("xiaoa-update-"):
            raise ReleaseError("拒绝清理非更新临时目录")
        shutil.rmtree(resolved, ignore_errors=True)
        raise


def _prepared_config_path(config_path: str | Path) -> Path:
    config = Path(config_path).resolve()
    directory = config.parent
    if (directory.parent != Path(tempfile.gettempdir()).resolve()
            or not directory.name.startswith("xiaoa-update-")
            or config.name != "update-config.json"):
        raise ReleaseError("更新准备文件路径无效，请取消后重新下载")
    return config


def cancel_prepared_update(config_path: str | Path) -> None:
    """Discard only the explicitly validated update workspace, never app data."""
    if not config_path:
        return
    directory = _prepared_config_path(config_path).parent
    if not directory.exists():
        return
    # Prepared workspaces contain only files. Never follow a nested directory.
    for entry in directory.iterdir():
        if entry.is_dir():
            raise ReleaseError("更新临时目录含未知子目录，已停止自动清理")
        entry.unlink(missing_ok=True)
    directory.rmdir()


def restart_prepared_update(config_path: str | Path, *, executable: str | Path,
                            parent_pid: int, backend_pid: int, timeout: float = 15) -> dict:
    """Validate the staged EXE again and require the independent worker to acknowledge."""
    if os.name != "nt":
        raise ReleaseError("正式版自动更新目前仅支持 Windows")
    config = _prepared_config_path(config_path)
    directory = config.parent
    value = json.loads(config.read_text(encoding="utf-8"))
    if not executable or parent_pid < 0 or backend_pid < 0:
        raise ReleaseError("当前程序更新上下文无效")
    target = Path(executable).resolve()
    downloaded = Path(value["Downloaded"]).resolve()
    if (target != Path(value["Target"]).resolve() or not target.is_file()
            or downloaded.parent != directory or not downloaded.is_file()
            or Path(value["WorkDir"]).resolve() != directory
            or Path(value["Backup"]).resolve().parent != target.parent
            or Path(value["Backup"]).name != target.name + f".backup-{directory.name}"
            or not _SHA256_RE.fullmatch(value.get("ExpectedSha256", ""))):
        raise ReleaseError("更新文件不可用，请取消后重新下载")
    with downloaded.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != value["ExpectedSha256"]:
            raise ReleaseError("安装包 SHA-256 校验失败，请取消后重新下载")
    import psutil
    old_processes = {}
    for pid in (parent_pid, backend_pid):
        if pid <= 0:
            continue
        try:
            process = psutil.Process(pid)
            for child in (process, *process.children(recursive=True)):
                old_processes[child.pid] = {"pid": child.pid, "created": child.create_time()}
            # Portable's outer NSIS launcher also holds the actual EXE open.
            for ancestor in process.parents():
                if Path(ancestor.exe()).resolve() == target:
                    old_processes[ancestor.pid] = {"pid": ancestor.pid, "created": ancestor.create_time()}
        except psutil.NoSuchProcess:
            pass
    value.update(ParentPid=int(parent_pid), BackendPid=int(backend_pid), OldProcesses=list(old_processes.values()))
    config.write_text(json.dumps(value), encoding="utf-8")
    ready = directory / "worker-ready"
    approved = directory / "restart-approved"
    ready.unlink(missing_ok=True)
    approved.unlink(missing_ok=True)
    log = Path(value["LogPath"])
    log.parent.mkdir(parents=True, exist_ok=True)
    helper = directory / "install-update.ps1"
    # Keep parser/startup errors: detached stdout/stderr must not disappear.
    worker_env = os.environ.copy()
    # 只恢复 Electron 启动时的临时目录，不把程序运行时解压进任务隔离区。
    launch_temp = Path(worker_env.get("XIAOA_LAUNCH_TEMP", ""))
    if launch_temp.is_absolute() and launch_temp.is_dir():
        for name in ("TEMP", "TMP", "TMPDIR"):
            worker_env[name] = str(launch_temp)
    with log.open("ab") as output:
        worker = subprocess.Popen([
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(helper),
            "-ConfigPath", str(config),
        ], env=worker_env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
           stdin=subprocess.DEVNULL, stdout=output, stderr=output, close_fds=True)
    deadline = time.monotonic() + timeout
    while not ready.exists() and worker.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if not ready.exists() or worker.poll() is not None:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        raise ReleaseError("更新器未能启动，请查看 data/update.log；旧版未关闭")
    approved.write_text("approved", encoding="ascii")
    return {"version": value["ExpectedVersion"], "target": str(target)}
