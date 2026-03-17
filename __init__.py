"""
Chrome Profiles Plugin
======================

Registers a ``browser_profile`` tool that switches the agent's browser tools
to a named Chrome instance via CDP.  Supports local (auto-launch) and remote
(reachability-gated) profiles defined in ``profiles.yaml``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

_CONFIG_FILE = "config.yaml"
_cached_config: Optional[Dict[str, Any]] = None


def _plugin_dir() -> Path:
    """Return the directory this plugin lives in."""
    return Path(__file__).parent


def _load_config(reload: bool = False) -> Dict[str, Any]:
    """Load and cache profiles.yaml from the plugin directory."""
    global _cached_config
    if _cached_config is not None and not reload:
        return _cached_config

    config_path = _plugin_dir() / _CONFIG_FILE
    if not config_path.exists():
        _cached_config = {}
        return _cached_config

    if yaml is None:
        logger.error("PyYAML not available — cannot load profiles.yaml")
        _cached_config = {}
        return _cached_config

    with open(config_path) as f:
        _cached_config = yaml.safe_load(f) or {}
    return _cached_config


def _get_profiles() -> Dict[str, Dict[str, Any]]:
    """Return the profiles dict from config."""
    return _load_config().get("profiles", {})


# ---------------------------------------------------------------------------
# Active profile tracking
# ---------------------------------------------------------------------------

_active_profile: Optional[str] = None


# ---------------------------------------------------------------------------
# Chrome binary resolution
# ---------------------------------------------------------------------------

_CHROME_SEARCH_NAMES = [
    "google-chrome",
    "google-chrome-stable",
    "google-chrome-beta",
    "chromium-browser",
    "chromium",
]


def _find_chrome(profile_cfg: Dict[str, Any]) -> Optional[str]:
    """Resolve Chrome binary path.

    Priority: profile-level chrome_binary > top-level chrome_binary > PATH.
    """
    # Per-profile override
    binary = profile_cfg.get("chrome_binary")
    if binary:
        expanded = os.path.expanduser(binary)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
        logger.warning("Profile chrome_binary not found/executable: %s", expanded)

    # Top-level override
    config = _load_config()
    binary = config.get("chrome_binary")
    if binary:
        expanded = os.path.expanduser(binary)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
        logger.warning("Top-level chrome_binary not found/executable: %s", expanded)

    # Auto-detect from PATH
    for name in _CHROME_SEARCH_NAMES:
        found = shutil.which(name)
        if found:
            return found

    return None


# ---------------------------------------------------------------------------
# Port probing
# ---------------------------------------------------------------------------

def _is_port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """Check if a TCP port is reachable."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
        return True
    except (OSError, socket.timeout):
        return False


# ---------------------------------------------------------------------------
# Chrome launch (local only)
# ---------------------------------------------------------------------------

def _launch_chrome(chrome_binary: str, data_dir: str, port: int) -> bool:
    """Launch Chrome with remote debugging.  Returns True if port comes up."""
    expanded_dir = os.path.expanduser(data_dir)

    cmd = [
        chrome_binary,
        f"--user-data-dir={expanded_dir}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    logger.info("Launching Chrome: %s", " ".join(cmd))

    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        logger.error("Failed to launch Chrome: %s", e)
        return False

    # Poll for port readiness
    for _ in range(20):  # 10 seconds max
        time.sleep(0.5)
        if _is_port_open("127.0.0.1", port, timeout=1.0):
            return True

    return False


# ---------------------------------------------------------------------------
# Cleanup helper
# ---------------------------------------------------------------------------

def _flush_browser_sessions():
    """Clear active browser sessions so the next tool call uses the new CDP URL."""
    try:
        from tools.browser_tool import cleanup_all_browsers
        cleanup_all_browsers()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------

def _list_profiles_response() -> str:
    """Build a JSON response listing all profiles and the active one."""
    global _active_profile
    profiles = _get_profiles()
    entries = []
    for name, cfg in profiles.items():
        entry: Dict[str, Any] = {
            "name": name,
            "type": cfg.get("type", "local"),
            "port": cfg.get("port"),
            "active": name == _active_profile,
        }
        if cfg.get("type") == "remote":
            entry["host"] = cfg.get("host", "")
        else:
            entry["data_dir"] = cfg.get("data_dir", "")
        entries.append(entry)

    return json.dumps({
        "profiles": entries,
        "active": _active_profile,
        "cdp_url": os.environ.get("BROWSER_CDP_URL", ""),
    })


def browser_profile(args: Dict[str, Any], **kwargs) -> str:
    """Switch browser tools to a named Chrome profile via CDP.

    - Local profiles: auto-launches Chrome if not running.
    - Remote profiles: fails if host:port is unreachable.
    - No arguments: lists available profiles and shows which is active.
    """
    global _active_profile

    name = args.get("name", "").strip()

    # No name → list profiles
    if not name:
        return _list_profiles_response()

    # Reload config each time to pick up edits without restart
    _load_config(reload=True)
    profiles = _get_profiles()

    if name not in profiles:
        available = ", ".join(profiles.keys()) if profiles else "none"
        return json.dumps({
            "error": f"Unknown profile: '{name}'. Available: {available}",
        })

    cfg = profiles[name]
    profile_type = cfg.get("type", "local")
    port = cfg.get("port")

    if not port:
        return json.dumps({"error": f"Profile '{name}' has no port configured"})

    # --- REMOTE ---
    if profile_type == "remote":
        host = cfg.get("host", "")
        if not host:
            return json.dumps({"error": f"Remote profile '{name}' has no host configured"})

        if not _is_port_open(host, port):
            return json.dumps({
                "error": f"Remote profile '{name}' is unreachable at {host}:{port}",
                "profile": name,
                "host": host,
                "port": port,
            })

        _flush_browser_sessions()
        cdp_url = f"http://{host}:{port}"
        os.environ["BROWSER_CDP_URL"] = cdp_url
        _active_profile = name

        return json.dumps({
            "success": True,
            "profile": name,
            "type": "remote",
            "cdp_url": cdp_url,
            "message": f"Connected to remote Chrome at {host}:{port}",
        })

    # --- LOCAL ---
    if not _is_port_open("127.0.0.1", port):
        # Not running — try to launch
        chrome_binary = _find_chrome(cfg)
        if not chrome_binary:
            return json.dumps({
                "error": (
                    f"Profile '{name}' is not running on port {port} and no Chrome "
                    "binary found. Set chrome_binary in profiles.yaml or install Chrome."
                ),
                "profile": name,
                "port": port,
            })

        data_dir = cfg.get("data_dir", "")
        if not data_dir:
            return json.dumps({
                "error": f"Local profile '{name}' has no data_dir configured",
            })

        launched = _launch_chrome(chrome_binary, data_dir, port)
        if not launched:
            return json.dumps({
                "error": f"Launched Chrome for '{name}' but port {port} didn't come up within 10s",
                "profile": name,
                "port": port,
            })

    _flush_browser_sessions()
    cdp_url = f"http://127.0.0.1:{port}"
    os.environ["BROWSER_CDP_URL"] = cdp_url
    _active_profile = name

    return json.dumps({
        "success": True,
        "profile": name,
        "type": "local",
        "cdp_url": cdp_url,
        "message": f"Browser tools connected to '{name}' on port {port}",
    })


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

BROWSER_PROFILE_SCHEMA = {
    "name": "browser_profile",
    "description": (
        "Switch the agent's browser tools (browser_navigate, browser_click, "
        "browser_snapshot, etc.) to a named Chrome profile via CDP. "
        "Local profiles auto-launch Chrome if not running. "
        "Remote profiles fail if the host is unreachable. "
        "Call with no name to list available profiles and see which is active. "
        "Must be called before browser_navigate when you need a specific "
        "authenticated context (e.g. a logged-in Gmail, AWS console, etc.)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Profile name to switch to (e.g. 'work', 'personal'). "
                    "Omit to list all available profiles."
                ),
            },
        },
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx):
    """Called by the Hermes plugin system on load."""
    ctx.register_tool(
        name="browser_profile",
        toolset="browser",
        schema=BROWSER_PROFILE_SCHEMA,
        handler=browser_profile,
        description="Switch browser tools to a named Chrome profile via CDP",
        emoji="🔀",
    )
    logger.info("chrome-profiles plugin loaded — %d profiles available",
                len(_get_profiles()))
