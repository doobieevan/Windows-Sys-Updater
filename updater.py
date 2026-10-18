from __future__ import annotations

from dataclasses import dataclass
import ctypes
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Callable, Iterable

LOGGER = logging.getLogger("appupdater")
_UPDATE_LOCK = threading.Lock()
_PROTECTED_PROCESS_NAMES = {
    "system", "registry", "smss", "csrss", "wininit", "services", "lsass",
    "svchost", "winlogon", "dwm", "system idle process", "idle", "explorer",
    "appupdater", "winget", "powershell", "pwsh", "cmd", "conhost",
}


@dataclass(frozen=True)
class UpdateItem:
    name: str
    installed_version: str
    available_version: str = ""
    package_id: str = ""
    source: str = "Windows registry"
    kind: str = "application"
    executable: str = ""

    @property
    def update_available(self) -> bool:
        if not self.available_version or self.available_version.casefold() in {"unknown", "n/a", "—"}:
            return False
        return _version_key(self.available_version) > _version_key(self.installed_version)


@dataclass(frozen=True)
class UpdateResult:
    item: UpdateItem
    success: bool
    detail: str


def _version_key(value: str) -> tuple[int, ...]:
    return tuple(int(number) for number in re.findall(r"\d+", value or "")) or (0,)


def _run(command: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(command, 127, "", f"Command not found: {command[0]} ({exc})")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124, "", f"Command timed out after {timeout} seconds")


def _powershell_json(script: str) -> list[dict]:
    result = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], 90)
    if result.returncode != 0 or not result.stdout.strip():
        LOGGER.warning("PowerShell scan failed: %s", result.stderr.strip())
        return []
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        LOGGER.warning("PowerShell returned invalid JSON")
        return []
    return [value] if isinstance(value, dict) else value if isinstance(value, list) else []


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def scan_installed_apps() -> list[UpdateItem]:
    script = r"""
$paths = @(
 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
 'HKLM:\Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*'
)
Get-ItemProperty $paths -ErrorAction SilentlyContinue |
 Where-Object { $_.DisplayName -and $_.DisplayVersion } |
 ForEach-Object { [pscustomobject]@{name=$_.DisplayName; version=$_.DisplayVersion} } |
 ConvertTo-Json -Compress
"""
    unique: dict[str, UpdateItem] = {}
    for entry in _powershell_json(script):
        name = str(entry.get("name", "")).strip()
        version = str(entry.get("version", "")).strip()
        if name and version:
            unique.setdefault(_normalise(name), UpdateItem(name, version))
    return sorted(unique.values(), key=lambda item: item.name.casefold())


def scan_drivers() -> list[UpdateItem]:
    script = r"""
Get-CimInstance Win32_PnPSignedDriver -ErrorAction SilentlyContinue |
 Where-Object { $_.DeviceName -and $_.DriverVersion } |
 Group-Object DeviceName |
 ForEach-Object {
   $item=$_.Group | Sort-Object DriverDate -Descending | Select-Object -First 1
   [pscustomobject]@{name=$item.DeviceName; version=$item.DriverVersion; provider=$item.DriverProviderName; deviceid=$item.DeviceID}
 } | ConvertTo-Json -Compress
"""
    unique: dict[str, UpdateItem] = {}
    for entry in _powershell_json(script):
        name, version = str(entry.get("name", "")).strip(), str(entry.get("version", "")).strip()
        if name and version:
            unique.setdefault(_normalise(name), UpdateItem(name, version, source=str(entry.get("provider", "Windows")), kind="driver"))
    return sorted(unique.values(), key=lambda item: item.name.casefold())


def _winget_json() -> list[dict]:
    result = _run(["winget", "upgrade", "--include-unknown", "--accept-source-agreements", "--disable-interactivity", "--format", "json"], 180)
    if result.returncode not in (0, 1) or not result.stdout.strip():
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("Data", "data", "Packages", "packages", "Sources", "sources"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def _winget_table() -> list[dict]:
    result = _run(["winget", "upgrade", "--include-unknown", "--accept-source-agreements", "--disable-interactivity"], 180)
    if result.returncode not in (0, 1):
        return []
    entries: list[dict] = []
    for line in result.stdout.splitlines():
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) >= 5 and parts[0].casefold() not in {"name", "-", "no", "the"}:
            entries.append({"Name": parts[0], "Id": parts[1], "Version": parts[2], "AvailableVersion": parts[3], "Source": parts[4]})
    return entries


def winget_updates() -> dict[str, tuple[str, str, str]]:
    entries = _winget_json() or _winget_table()
    updates: dict[str, tuple[str, str, str]] = {}
    for entry in entries:
        name = str(entry.get("Name") or entry.get("name") or entry.get("PackageName") or entry.get("packageName") or "").strip()
        package_id = str(entry.get("Id") or entry.get("id") or entry.get("PackageIdentifier") or entry.get("packageIdentifier") or "").strip()
        installed = str(entry.get("Version") or entry.get("version") or entry.get("InstalledVersion") or entry.get("installedVersion") or "").strip()
        available = str(entry.get("AvailableVersion") or entry.get("availableVersion") or entry.get("Available") or entry.get("available") or "").strip()
        if name and package_id and available:
            updates[_normalise(name)] = (installed, available, package_id)
    return updates


def check_for_updates(include_drivers: bool = True) -> list[UpdateItem]:
    available = winget_updates()
    items: dict[tuple[str, str], UpdateItem] = {}
    for item in scan_installed_apps():
        match = available.get(_normalise(item.name))
        if match:
            _, available_version, package_id = match
            item = UpdateItem(item.name, item.installed_version, available_version, package_id, "winget", item.kind)
        items[(_normalise(item.name), item.kind)] = item
    for name_key, (installed, available_version, package_id) in available.items():
        key = (name_key, "application")
        if key not in items:
            items[key] = UpdateItem(name_key, installed or "unknown", available_version, package_id, "winget")
    if include_drivers:
        for item in scan_drivers():
            items[(_normalise(item.name), item.kind)] = item
    return sorted(items.values(), key=lambda item: item.name.casefold())


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return os.name != "nt" and os.geteuid() == 0


def _process_name_candidates(item: UpdateItem) -> set[str]:
    values = {item.name}
    if item.package_id:
        values.add(item.package_id.rsplit(".", 1)[-1])
    if item.executable:
        values.add(Path(item.executable).stem)
    return {_normalise(value) for value in values if value}


def _close_application_processes(item: UpdateItem, logger: Callable[[str, str], None] | None = None) -> str:
    """Close only an exact application match; never run for drivers or protected names."""
    if item.kind == "driver":
        return "Process shutdown skipped for driver update"
    candidates = _process_name_candidates(item) - {_normalise(name) for name in _PROTECTED_PROCESS_NAMES}
    if not candidates:
        return "No safe process name matched"
    names = ",".join(sorted(candidates)).replace("'", "''")
    script = f"""
$names = @('{names}'.Split(','))
$closed = @()
Get-Process -ErrorAction SilentlyContinue | ForEach-Object {{
    $normal = ($_.ProcessName -replace '[^a-zA-Z0-9]', '').ToLowerInvariant()
    if ($names -contains $normal -and $_.Id -ne $PID) {{
        try {{
            if ($_.MainWindowHandle -ne 0) {{ [void]$_.CloseMainWindow() }}
            $closed += [pscustomobject]@{{name=$_.ProcessName; pid=$_.Id}}
        }} catch {{}}
    }}
}}
Start-Sleep -Seconds 5
foreach ($entry in $closed) {{
    if (Get-Process -Id $entry.pid -ErrorAction SilentlyContinue) {{
        try {{ Stop-Process -Id $entry.pid -Force -ErrorAction Stop }} catch {{}}
    }}
}}
$closed | ConvertTo-Json -Compress
"""
    result = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], 20)
    if result.returncode != 0 or not result.stdout.strip():
        detail = "No matching application process was closed"
    else:
        try:
            closed = json.loads(result.stdout)
            closed = closed if isinstance(closed, list) else [closed]
        except json.JSONDecodeError:
            closed = []
        detail = f"Closed {len(closed)} matching application process(es) before update"
    if logger:
        logger("INFO", f"{detail}: {item.name}")
    return detail


    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return os.name != "nt" and os.geteuid() == 0


def _result_message(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout.strip() or result.stderr.strip() or f"Process exited with code {result.returncode}")[-1600:]


def install_update(item: UpdateItem, logger: Callable[[str, str], None] | None = None, retries: int = 2) -> tuple[bool, str]:
    if not item.update_available:
        return False, "The item has no confirmed newer version"
    if item.kind == "driver":
        return False, "Driver updates are detected here, but installation is disabled until Windows Update provides a verified package"
    if not item.package_id or not re.fullmatch(r"[A-Za-z0-9_.-]+", item.package_id):
        return False, "No trusted winget package identifier was found"
    if not shutil.which("winget"):
        return False, "winget is not installed or is unavailable on PATH"
    if not is_admin():
        return False, "Run AppUpdater as administrator before installing updates"
    _close_application_processes(item, logger)
    command = ["winget", "upgrade", "--id", item.package_id, "--exact", "--silent", "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"]
    result = None
    for attempt in range(retries + 1):
        result = _run(command, 900)
        if result.returncode == 0:
            break
        if attempt < retries:
            time.sleep(2 ** attempt)
    assert result is not None
    detail = _result_message(result)
    ok = result.returncode == 0
    if logger:
        logger("INFO" if ok else "ERROR", f"update_{'installed' if ok else 'failed'} {item.name}: {detail}")
    return ok, detail


def install_updates(
    items: Iterable[UpdateItem],
    logger: Callable[[str, str], None] | None = None,
    on_start: Callable[[int, int, UpdateItem], None] | None = None,
) -> list[UpdateResult]:
    """Install selected updates serially; never run two installers concurrently."""
    selected = list(items)
    results: list[UpdateResult] = []
    with _UPDATE_LOCK:
        total = len(selected)
        for number, item in enumerate(selected, 1):
            if on_start:
                on_start(number, total, item)
            success, detail = install_update(item, logger)
            results.append(UpdateResult(item, success, detail))
    return results


def clean_update_cache(logger: Callable[[str, str], None] | None = None) -> list[str]:
    paths = [Path(tempfile.gettempdir()) / "AppUpdater"]
    removed: list[str] = []
    for path in paths:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            if not path.exists():
                removed.append(str(path))
    if logger:
        logger("INFO", f"update_cache_cleaned count={len(removed)}")
    return removed
