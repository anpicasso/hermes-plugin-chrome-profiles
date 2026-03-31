"""
Chrome Profiles Plugin
======================

Registers a ``browser_profile`` tool that switches the agent's browser tools
to a named Chrome instance via CDP.  Supports local (auto-launch) and remote
(reachability-gated) profiles defined in ``config.yaml``.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
import urllib.error
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
    """Load and cache config.yaml from the plugin directory."""
    global _cached_config
    if _cached_config is not None and not reload:
        return _cached_config

    config_path = _plugin_dir() / _CONFIG_FILE
    if not config_path.exists():
        _cached_config = {}
        return _cached_config

    if yaml is None:
        logger.error("PyYAML not available — cannot load config.yaml")
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
# Process tracking (BUG 2): Track launched Chrome PIDs to prevent orphans
# ---------------------------------------------------------------------------
# Maps profile name -> PID of launched Chrome process
_chrome_pids: Dict[str, int] = {}

def _cleanup_chrome_processes():
    """atexit handler: terminate all tracked Chrome processes on shutdown."""
    for profile_name, pid in list(_chrome_pids.items()):
        try:
            import signal
            os.kill(pid, signal.SIGTERM)
            logger.info("Terminated Chrome process for profile '%s' (PID %d)", profile_name, pid)
        except ProcessLookupError:
            pass  # Process already dead
        except Exception as e:
            logger.warning("Failed to terminate Chrome for '%s': %s", profile_name, e)

atexit.register(_cleanup_chrome_processes)

def _is_pid_alive(pid: int) -> bool:
    """Check if a process with given PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Concurrency control (BUG 3): Per-profile launch locks to prevent TOCTOU races
# ---------------------------------------------------------------------------
_profile_locks: Dict[str, threading.Lock] = {}

def _get_profile_lock(profile_name: str) -> threading.Lock:
    """Get or create a lock for the given profile to serialize launch attempts."""
    if profile_name not in _profile_locks:
        _profile_locks[profile_name] = threading.Lock()
    return _profile_locks[profile_name]


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

_EDGE_SEARCH_NAMES = [
    "microsoft-edge",
    "microsoft-edge-stable",
    "edge",
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


def _find_edge(profile_cfg: Dict[str, Any]) -> Optional[str]:
    """Resolve Edge binary path.

    Priority: profile-level edge_binary > top-level edge_binary > PATH.
    """
    # Per-profile override
    binary = profile_cfg.get("edge_binary")
    if binary:
        expanded = os.path.expanduser(binary)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
        logger.warning("Profile edge_binary not found/executable: %s", expanded)

    # Top-level override
    config = _load_config()
    binary = config.get("edge_binary")
    if binary:
        expanded = os.path.expanduser(binary)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
        logger.warning("Top-level edge_binary not found/executable: %s", expanded)

    # Auto-detect from PATH
    for name in _EDGE_SEARCH_NAMES:
        found = shutil.which(name)
        if found:
            return found

    return None


def _find_browser(profile_cfg: Dict[str, Any]) -> tuple[Optional[str], str]:
    """Resolve browser binary path and type.
    
    Returns: (binary_path, browser_type) where browser_type is 'chrome' or 'edge'
    Priority: Check browser_type in config first, then try auto-detect.
    """
    browser_type = profile_cfg.get("browser_type", "auto").lower()
    
    # If explicitly set to edge, only look for Edge
    if browser_type == "edge":
        edge_binary = _find_edge(profile_cfg)
        if edge_binary:
            return edge_binary, "edge"
        return None, "edge"
    
    # If explicitly set to chrome, only look for Chrome
    if browser_type == "chrome":
        chrome_binary = _find_chrome(profile_cfg)
        if chrome_binary:
            return chrome_binary, "chrome"
        return None, "chrome"
    
    # Auto-detect: Try Chrome first, then Edge
    chrome_binary = _find_chrome(profile_cfg)
    if chrome_binary:
        return chrome_binary, "chrome"
    
    edge_binary = _find_edge(profile_cfg)
    if edge_binary:
        return edge_binary, "edge"
    
    return None, "unknown"


# ---------------------------------------------------------------------------
# Port probing
# ---------------------------------------------------------------------------

def _is_port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """Check if a TCP port is reachable."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect((host, port))
        return True
    except (OSError, socket.timeout):
        return False
    finally:
        # Always close socket to prevent leaks, even on connection error
        s.close()


def _is_cdp_ready(host: str, port: int, timeout: float = 2.0) -> bool:
    """Check if CDP endpoint is responding with valid debugger URL.

    Fetches /json/version and verifies 'webSocketDebuggerUrl' is present.
    This ensures not just the port is open, but Chrome is actually ready for CDP.
    """
    try:
        url = f"http://{host}:{port}/json/version"
        req = urllib.request.Request(url, headers={"Connection": "close"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
            return "webSocketDebuggerUrl" in data
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError):
        return False


# ---------------------------------------------------------------------------
# Chrome launch (local only)
# ---------------------------------------------------------------------------

def _launch_chrome(chrome_binary: str, data_dir: str, port: int, profile_name: str = "") -> bool:
    """Launch Chrome with remote debugging.  Returns True if port comes up."""
    global _chrome_pids

    expanded_dir = os.path.expanduser(data_dir)

    # BUG 6: Log stderr to file for debugging launch failures
    log_file_path = os.path.join(expanded_dir, "chrome-launch.log")

    cmd = [
        chrome_binary,
        f"--user-data-dir={expanded_dir}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    logger.info("Launching Chrome: %s", " ".join(cmd))

    try:
        # BUG 6: Capture stderr to log file instead of DEVNULL
        with open(log_file_path, "w") as log_file:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=log_file,
                start_new_session=True,
            )
            # BUG 2: Track PID for cleanup on shutdown
            if profile_name:
                _chrome_pids[profile_name] = process.pid
                logger.debug("Tracking Chrome PID %d for profile '%s'", process.pid, profile_name)
    except Exception as e:
        logger.error("Failed to launch Chrome: %s", e)
        return False

    # BUG 12: Read configurable timeout from config, default to 10s
    config = _load_config()
    launch_timeout = config.get("launch_timeout", 10)
    poll_interval = 0.5
    max_attempts = int(launch_timeout / poll_interval)

    # BUG 4: Poll for CDP readiness (not just port open)
    for attempt in range(max_attempts):
        time.sleep(poll_interval)
        if _is_cdp_ready("127.0.0.1", port, timeout=1.0):
            logger.info("Chrome ready on port %d after %.1fs", port, (attempt + 1) * poll_interval)
            return True

    # BUG 6: If timeout, read and include log contents in error message
    logger.warning("Chrome launch timeout after %ds. Log contents:")
    try:
        with open(log_file_path, "r") as log_file:
            log_contents = log_file.read()
            if log_contents:
                for line in log_contents.strip().split("\n"):
                    logger.warning("  %s", line)
            else:
                logger.warning("  (empty log file)")
    except Exception as read_err:
        logger.warning("  (could not read log: %s)", read_err)

    return False


def _launch_edge(edge_binary: str, profile_directory: str, port: int, profile_name: str = "") -> bool:
    """Launch Microsoft Edge with remote debugging. Returns True if port comes up.
    
    Edge uses --profile-directory instead of --user-data-dir.
    """
    global _chrome_pids

    # Use /tmp for Edge logs since Edge doesn't use data_dir like Chrome
    log_file_path = f"/tmp/edge-{profile_name}-launch.log"

    cmd = [
        edge_binary,
        f"--profile-directory={profile_directory}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    logger.info("Launching Edge: %s", " ".join(cmd))

    try:
        with open(log_file_path, "w") as log_file:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=log_file,
                start_new_session=True,
            )
            if profile_name:
                _chrome_pids[profile_name] = process.pid
                logger.debug("Tracking Edge PID %d for profile '%s'", process.pid, profile_name)
    except Exception as e:
        logger.error("Failed to launch Edge: %s", e)
        return False

    config = _load_config()
    launch_timeout = config.get("launch_timeout", 10)
    poll_interval = 0.5
    max_attempts = int(launch_timeout / poll_interval)

    for attempt in range(max_attempts):
        time.sleep(poll_interval)
        if _is_cdp_ready("127.0.0.1", port, timeout=1.0):
            logger.info("Edge ready on port %d after %.1fs", port, (attempt + 1) * poll_interval)
            return True

    logger.warning("Edge launch timeout after %ds. Log contents:", launch_timeout)
    try:
        with open(log_file_path, "r") as log_file:
            log_contents = log_file.read()
            if log_contents:
                for line in log_contents.strip().split("\n"):
                    logger.warning("  %s", line)
            else:
                logger.warning("  (empty log file)")
    except Exception as read_err:
        logger.warning("  (could not read log: %s)", read_err)

    return False


# ---------------------------------------------------------------------------
# Cleanup helper
# ---------------------------------------------------------------------------

def _flush_browser_sessions():
    """Clear active browser sessions so the next tool call uses the new CDP URL."""
    try:
        from tools.browser_tool import cleanup_all_browsers
        cleanup_all_browsers()
    except ImportError:
        pass  # Expected when browser_tool not available
    except Exception as e:
        logger.warning("Failed to flush browser sessions: %s", e)


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------

def _list_profiles_response() -> str:
    """Build a JSON response listing all profiles and the active one."""
    global _active_profile
    profiles = _get_profiles()
    entries = []

    # BUG 7: Check if active profile is still reachable, clear if not
    if _active_profile and _active_profile in profiles:
        cfg = profiles[_active_profile]
        port = cfg.get("port")
        host = "127.0.0.1" if cfg.get("type", "local") == "local" else cfg.get("host", "")
        if port and not _is_cdp_ready(host, port, timeout=1.0):
            logger.info("Active profile '%s' no longer reachable, clearing", _active_profile)
            _active_profile = None

    for name, cfg in profiles.items():
        port = cfg.get("port")
        host = "127.0.0.1" if cfg.get("type", "local") == "local" else cfg.get("host", "")
        reachable = _is_cdp_ready(host, port, timeout=0.5) if port else False

        entry: Dict[str, Any] = {
            "name": name,
            "type": cfg.get("type", "local"),
            "port": port,
            "active": name == _active_profile,
            "reachable": reachable,
            "browser_type": cfg.get("browser_type", "auto"),
        }
        if cfg.get("type") == "remote":
            entry["host"] = cfg.get("host", "")
        else:
            # Show appropriate dir config based on browser type
            if cfg.get("browser_type") == "edge":
                entry["profile_directory"] = cfg.get("profile_directory", "Default")
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

    # BUG 11: Return clear error if PyYAML is not available
    if yaml is None:
        return json.dumps({
            "error": "PyYAML not installed. Install with: pip install pyyaml",
        })

    name = args.get("name", "").strip()

    # BUG 8: Reload config in list mode too to pick up edits
    if not name:
        _load_config(reload=True)
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

    # BUG 10: Validate port range
    if not isinstance(port, int) or not (1 <= port <= 65535):
        return json.dumps({"error": f"Profile '{name}' has invalid port {port!r} (must be 1-65535)"})

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
    # BUG 3: Use per-profile lock to prevent TOCTOU race conditions
    # Two concurrent calls could both see port closed and both launch Chrome
    profile_lock = _get_profile_lock(name)
    with profile_lock:
        # Re-check port inside lock - another thread may have launched Chrome
        if not _is_cdp_ready("127.0.0.1", port, timeout=1.0):
            # BUG 2: Check if we have a tracked PID that's still alive
            existing_pid = _chrome_pids.get(name)
            if existing_pid and _is_pid_alive(existing_pid):
                logger.info("Profile '%s' has alive PID %d, waiting for it to be ready", name, existing_pid)
                # Wait for existing Chrome to become ready
                for _ in range(20):
                    time.sleep(0.5)
                    if _is_cdp_ready("127.0.0.1", port, timeout=1.0):
                        break
                else:
                    # PID exists but Chrome not responding - might be stuck, proceed to try launch
                    logger.warning("Tracked PID %d for '%s' not responding, will attempt new launch", existing_pid, name)
                    del _chrome_pids[name]

            if not _is_cdp_ready("127.0.0.1", port, timeout=1.0):
                browser_binary, browser_type = _find_browser(cfg)
                if not browser_binary:
                    return json.dumps({
                        "error": (
                            f"Profile '{name}' is not running on port {port} and no browser "
                            "binary found. Set chrome_binary or edge_binary in config.yaml, "
                            "or install Chrome/Edge."
                        ),
                        "profile": name,
                        "port": port,
                    })

                if browser_type == "edge":
                    # Edge uses profile_directory instead of data_dir
                    profile_directory = cfg.get("profile_directory", "Default")
                    launched = _launch_edge(browser_binary, profile_directory, port, profile_name=name)
                    if not launched:
                        config = _load_config()
                        timeout = config.get("launch_timeout", 10)
                        return json.dumps({
                            "error": f"Launched Edge for '{name}' but port {port} didn't come up within {timeout}s",
                            "profile": name,
                            "port": port,
                            "browser": "edge",
                        })
                else:
                    # Chrome uses data_dir
                    data_dir = cfg.get("data_dir", "")
                    if not data_dir:
                        return json.dumps({
                            "error": f"Local profile '{name}' has no data_dir configured",
                        })

                    launched = _launch_chrome(browser_binary, data_dir, port, profile_name=name)
                    if not launched:
                        config = _load_config()
                        timeout = config.get("launch_timeout", 10)
                        return json.dumps({
                            "error": f"Launched Chrome for '{name}' but port {port} didn't come up within {timeout}s",
                            "profile": name,
                            "port": port,
                            "browser": "chrome",
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
        toolset="chrome_profiles",
        schema=BROWSER_PROFILE_SCHEMA,
        handler=browser_profile,
        description="Switch browser tools to a named Chrome profile via CDP",
        emoji="🔀",
    )
    logger.info("chrome-profiles plugin loaded — %d profiles available",
                len(_get_profiles()))
