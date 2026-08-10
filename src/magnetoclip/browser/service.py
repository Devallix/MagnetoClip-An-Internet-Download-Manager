"""Orchestrates the browser integration: launcher, manifests, extension.

The browser extension talks to MagnetoClip over native messaging. The browser
launches a host process on demand (``python main.py --browser-host``) using the
manifest registered in the browser's native-messaging directory. This service
writes those manifests, keeps a per-user copy of the extension ready to load,
and reports the state of everything.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from magnetoclip.core.events.bus import Events
from magnetoclip.resources import resource_path
from magnetoclip.services.logging import get_logger

from .integration.install import (
    ensure_extension_key,
    extension_id_from_public_key,
    host_manifest_path,
    public_key_base64,
)
from .integration.install import install as _install_manifests
from .integration.install import uninstall as _uninstall_manifests

log = get_logger(__name__)

if os.name == "nt":  # pragma: no cover - exercised on Windows
    import winreg
else:
    winreg = None

SUPPORTED_BROWSERS = ("chrome", "edge", "firefox", "brave", "vivaldi", "chromium")

_POLICY_KEY = {
    "chrome": r"SOFTWARE\Policies\Google\Chrome",
    "edge": r"SOFTWARE\Policies\Microsoft\Edge",
    "chromium": r"SOFTWARE\Policies\Chromium",
    "brave": r"SOFTWARE\Policies\BraveSoftware\Brave-Browser",
    "vivaldi": r"SOFTWARE\Policies\Vivaldi",
}

_FORCE_INSTALL_URL = "https://clients2.google.com/service/update2/crx"

_BROWSER_EXE = {
    "chrome": "chrome.exe",
    "edge": "msedge.exe",
    "firefox": "firefox.exe",
    "brave": "brave.exe",
    "vivaldi": "vivaldi.exe",
    "chromium": "chrome.exe",
}

_CANDIDATE_PATHS = {
    "chrome": (
        "{LOCALAPPDATA}\\Google\\Chrome\\Application\\chrome.exe",
        "{PROGRAMFILES}\\Google\\Chrome\\Application\\chrome.exe",
        "{PROGRAMFILES(X86)}\\Google\\Chrome\\Application\\chrome.exe",
    ),
    "edge": (
        "{PROGRAMFILES(X86)}\\Microsoft\\Edge\\Application\\msedge.exe",
        "{PROGRAMFILES}\\Microsoft\\Edge\\Application\\msedge.exe",
    ),
    "firefox": (
        "{PROGRAMFILES}\\Mozilla Firefox\\firefox.exe",
        "{PROGRAMFILES(X86)}\\Mozilla Firefox\\firefox.exe",
    ),
    "brave": ("{LOCALAPPDATA}\\BraveSoftware\\Brave-Browser\\Application\\brave.exe",),
    "vivaldi": ("{LOCALAPPDATA}\\Vivaldi\\Application\\vivaldi.exe",),
    "chromium": ("{LOCALAPPDATA}\\Chromium\\Application\\chrome.exe",),
}


class BrowserIntegrationService:
    def __init__(self, context) -> None:
        self.context = context

    # ----- host launcher -----

    def host_launcher_path(self) -> Path:
        return Path(self.context.config_dir) / "browser" / "host_launcher.cmd"

    def host_command(self) -> list[str]:
        """Command the launcher runs to serve native-messaging requests."""
        if getattr(sys, "frozen", False):
            return [sys.executable, "--browser-host"]
        import magnetoclip

        main_py = Path(magnetoclip.__file__).resolve().parent / "app" / "main.py"
        return [sys.executable, str(main_py), "--browser-host"]

    def ensure_launcher(self) -> Path:
        path = self.host_launcher_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tokens = " ".join(f'"{token}"' for token in self.host_command())
        content = f"@echo off\r\n{tokens} %*\r\n"
        path.write_text(content, encoding="utf-8")
        return path

    # ----- extension -----

    def extension_dir(self) -> Path:
        return Path(self.context.config_dir) / "browser_extension"

    def extension_id(self) -> str:
        _key_path, public_key = ensure_extension_key(self.context.data_dir)
        return extension_id_from_public_key(public_key)

    def install_extension(self) -> Path:
        """Copy the bundled extension to the config dir with the signing key."""
        target = self.extension_dir()
        target.mkdir(parents=True, exist_ok=True)
        source = resource_path("browser_extension")
        for entry in source.iterdir():
            destination = target / entry.name
            if entry.is_dir():
                shutil.copytree(entry, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(entry, destination)
        manifest_path = target / "manifest.json"
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        _key_path, public_key = ensure_extension_key(self.context.data_dir)
        manifest["key"] = public_key_base64(public_key)
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)
        log.info("browser_extension_prepared", path=str(target))
        return target

    # ----- browser detection -----

    def browser_install_path(self, browser: str) -> Path | None:
        if browser not in _BROWSER_EXE:
            return None
        registry_path = self._registry_default(browser)
        if registry_path is not None and registry_path.is_file():
            return registry_path
        for template in _CANDIDATE_PATHS.get(browser, ()):
            candidate = Path(_expand_env(template))
            if candidate.is_file():
                return candidate
        return None

    def detect_browsers(self) -> list[str]:
        return [
            name for name in SUPPORTED_BROWSERS
            if self.browser_install_path(name) is not None
        ]

    @staticmethod
    def _registry_default(browser: str) -> Path | None:
        if os.name != "nt":
            return None
        try:
            import winreg
        except ImportError:
            return None
        key = (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
            rf"\{_BROWSER_EXE[browser]}"
        )
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(root, key) as handle:
                    value, _ = winreg.QueryValueEx(handle, "")
            except OSError:
                continue
            return Path(value)
        return None

    # ----- install / uninstall -----

    def install(self, browsers: list[str]) -> dict[str, str]:
        self.ensure_launcher()
        results = _install_manifests(
            browsers,
            host_path=self.host_launcher_path(),
            data_dir=self.context.data_dir,
        )
        self._post_status()
        return results

    def uninstall(self, browsers: list[str]) -> dict[str, str]:
        results = _uninstall_manifests(browsers, self.context.data_dir)
        self._post_status()
        return results

    def ensure_installed(self) -> dict[str, str]:
        """Idempotent activation: launcher + extension + manifests for detected browsers."""
        self.ensure_launcher()
        self.install_extension()
        return self.install(self.detect_browsers())

    # ----- force install (policy) -----

    def force_install(self, browser: str) -> dict[str, str]:
        """Force-install the extension via browser policy.

        The policy entry uses the Chrome Web Store update URL, so the
        extension must be published there for the install to succeed. The
        extension ID is derived from our signing key, so it stays stable.
        """
        if browser not in _POLICY_KEY:
            return {
                "ok": False,
                "message": f"Auto-install is not supported for {browser}.",
            }
        extension_id = self.extension_id()
        value = f"{extension_id};{_FORCE_INSTALL_URL}"
        marker = Path(self.context.config_dir) / "browser" / "force_install_result.txt"
        try:
            _force_install_policy(
                winreg.HKEY_CURRENT_USER, browser, extension_id, _FORCE_INSTALL_URL
            )
            return {
                "ok": True,
                "message": f"Auto-install policy applied for {browser} (current user).",
            }
        except OSError:
            applied = _run_elevated_policy_script(
                browser, extension_id, _FORCE_INSTALL_URL, marker
            )
            if applied:
                return {
                    "ok": True,
                    "message": f"Auto-install policy applied for {browser} (all users).",
                }
            return {
                "ok": False,
                "message": (
                    f"Could not write the auto-install policy for {browser}. "
                    "Run MagnetoClip as administrator and try again."
                ),
            }

    def remove_force_install(self, browser: str) -> dict[str, str]:
        if browser not in _POLICY_KEY:
            return {"ok": False, "message": f"Unsupported browser: {browser}."}
        try:
            _remove_policy_entry(winreg.HKEY_CURRENT_USER, browser, self.extension_id())
            return {"ok": True, "message": f"Auto-install policy removed for {browser}."}
        except OSError:
            return {
                "ok": False,
                "message": "Remove the extension from the browser's extension page.",
            }

    # ----- status -----

    def status(self) -> dict:
        enabled = bool(self.context.settings.get("browser.integration_enabled", False))
        launcher = self.host_launcher_path()
        extension = self.extension_dir()
        extension_id = self.extension_id()
        browsers = {}
        for name in SUPPORTED_BROWSERS:
            manifest_path = host_manifest_path(name, self.context.data_dir)
            browsers[name] = {
                "installed": self.browser_install_path(name) is not None,
                "manifest": manifest_path.exists(),
                "path": str(manifest_path),
                "force_installed": _policy_installed(name, extension_id)
                if name in _POLICY_KEY
                else False,
            }
        return {
            "enabled": enabled,
            "capture_enabled": bool(
                self.context.settings.get("browser.capture_enabled", True)
            ),
            "launcher": str(launcher),
            "launcher_exists": launcher.exists(),
            "extension_dir": str(extension),
            "extension_ready": (extension / "manifest.json").exists(),
            "extension_id": extension_id,
            "browsers": browsers,
        }

    def _post_status(self) -> None:
        try:
            self.context.events.post(Events.BROWSER_STATUS_CHANGED, self.status())
        except Exception:  # noqa: BLE001 - status notifications must not break installs
            log.warning("browser_status_post_failed", exc_info=True)


def _expand_env(template: str) -> str:
    value = os.path.expandvars(template.replace("{", "%").replace("}", "%"))
    if value == template and template.startswith("~"):
        value = str(Path.home() / template[1:].lstrip("\\/"))
    return value


def _policy_key_path(browser: str) -> str:
    return _POLICY_KEY[browser] + r"\ExtensionInstallForcelist"


def _force_install_policy(root, browser: str, extension_id: str, url: str) -> None:
    """Write an ExtensionInstallForcelist entry under ``root`` (HKCU/HKLM)."""
    if winreg is None:
        raise OSError("registry not available")
    key_path = _policy_key_path(browser)
    with winreg.CreateKey(root, key_path) as handle:
        entries: list[tuple[str, str]] = []
        index = 0
        while True:
            try:
                name, data, _ = winreg.EnumValue(handle, index)
            except OSError:
                break
            entries.append((str(name), str(data)))
            index += 1
        for name, data in entries:
            if data.split(";", 1)[0] == extension_id:
                return
        next_index = 1
        used = {name for name, _ in entries}
        while str(next_index) in used:
            next_index += 1
        winreg.SetValueEx(
            handle, str(next_index), 0, winreg.REG_SZ, f"{extension_id};{url}"
        )


def _remove_policy_entry(root, browser: str, extension_id: str) -> None:
    if winreg is None:
        raise OSError("registry not available")
    key_path = _policy_key_path(browser)
    with winreg.CreateKey(root, key_path) as handle:
        removals = []
        index = 0
        while True:
            try:
                name, data, _ = winreg.EnumValue(handle, index)
            except OSError:
                break
            if str(data).split(";", 1)[0] == extension_id:
                removals.append(str(name))
            index += 1
        for name in removals:
            winreg.DeleteValue(handle, name)


def _policy_installed(browser: str, extension_id: str) -> bool:
    if winreg is None or browser not in _POLICY_KEY:
        return False
    key_path = _policy_key_path(browser)
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, key_path) as handle:
                index = 0
                while True:
                    try:
                        _name, data, _ = winreg.EnumValue(handle, index)
                    except OSError:
                        break
                    if str(data).split(";", 1)[0] == extension_id:
                        return True
                    index += 1
        except OSError:
            continue
    return False


def _run_elevated_policy_script(
    browser: str, extension_id: str, url: str, marker: Path
) -> bool:
    """Apply the policy for all users via an elevated PowerShell script."""
    if os.name != "nt":
        return False
    key_path = _policy_key_path(browser).replace("\\", "\\")
    hklm = "HKLM:\\" + key_path
    marker_path = str(marker)
    script = f"""
$ErrorActionPreference = "Stop"
$path = "{hklm}"
try {{
  New-Item -Path $path -Force | Out-Null
  $props = Get-ItemProperty -Path $path
  $found = $false
  foreach ($p in $props.PSObject.Properties) {{
    if ($p.Name -match '^\\d+$' -and $p.Value -like '{extension_id};*') {{ $found = $true }}
  }}
  if (-not $found) {{
    $idx = 1
    while ($props.PSObject.Properties.Name -contains ([string]$idx)) {{ $idx++ }}
    New-ItemProperty -Path $path -Name ([string]$idx) -Value '{extension_id};{url}' -PropertyType String -Force | Out-Null
  }}
  Set-Content -Path '{marker_path}' -Value 'ok' -Encoding Ascii
}} catch {{
  Set-Content -Path '{marker_path}' -Value 'error' -Encoding Ascii
}}
"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.Popen(
            [
                "powershell", "-NoProfile", "-Command",
                "Start-Process powershell -Verb RunAs -ArgumentList "
                f"'-NoProfile','-EncodedCommand','{encoded}'",
            ],
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except OSError:
        return False
    marker.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if marker.exists():
            try:
                return marker.read_text(encoding="ascii").strip() == "ok"
            except OSError:
                return False
        time.sleep(0.2)
    return False
