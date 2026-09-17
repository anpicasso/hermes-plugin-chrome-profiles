"""browser-profiles — point Hermes' browser tools at a named Chromium instance over CDP.

Registers ``browser_profile``: switch the *whole* browser stack (``browser_exec`` /
Browser Use CLI, the built-in ``browser_*`` tools, and ``browser_vault_*``) to a
named local or remote Chromium-family browser, so the agent inherits that
browser's cookies and logged-in sessions.

Mechanism is the same public contract ``/browser connect`` uses: set
``BROWSER_CDP_URL``, flush live sessions, attach the CDP supervisor. Everything
Hermes already knows how to do (find a browser binary, probe a CDP endpoint on
both loopbacks, pick a free port) is delegated to ``hermes_cli.browser_connect``.

Credits: the single-instance-lock failure mode handled by ``_holder_of`` was
diagnosed by github.com/LaansDole in a fork of v1.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

PLUGIN_ID = "chrome-profiles"

# Set by register(); the only handle to host config/state.
_ctx: Any = None

# profile name -> Popen of a browser WE launched (terminated on unload).
_launched: Dict[str, subprocess.Popen] = {}
# profile name -> launch lock. Two concurrent switches must not both launch.
_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

_active: Optional[str] = None

# Chromium flags for a Hermes-launched profile browser. Mirrors
# hermes_cli.browser_connect._chrome_debug_args plus the noise suppression
# tools/browser_tool_real_profile.py uses, minus --no-startup-window (a profile
# browser is usually meant to be visible).
_LAUNCH_FLAGS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-hang-monitor",
    "--disable-prompt-on-repost",
    "--disable-features=Translate",
)


# --------------------------------------------------------------------------- #
# host seams
# --------------------------------------------------------------------------- #

def _host():
    """Return the ``hermes_cli.browser_connect`` module (the public CDP/browser helpers)."""
    from hermes_cli import browser_connect
    return browser_connect


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _flush_sessions() -> None:
    """Drop live browser sessions so the next tool call dials the new endpoint.

    ``cleanup_all_browsers`` moved to ``tools.browser_tool_lifecycle``; importing it
    from ``tools.browser_tool`` still resolves through a deprecated compat shim.
    """
    try:
        from tools.browser_tool_lifecycle import cleanup_all_browsers
        cleanup_all_browsers()
    except Exception as e:  # a stale session is not worth failing the switch over
        logger.debug("browser session flush failed: %s", e)


def _supervisor(start: bool) -> None:
    """Attach/detach the per-task CDP supervisor so snapshots carry ``pending_dialogs``
    and ``frame_tree``, and ``browser_vault_fill`` has a secret-capable channel."""
    try:
        from tools import browser_tool_cdp
        (browser_tool_cdp._ensure_cdp_supervisor if start
         else browser_tool_cdp._stop_cdp_supervisor)("default")
    except Exception as e:
        logger.debug("CDP supervisor %s failed (non-fatal): %s", "start" if start else "stop", e)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

def _legacy_file_profiles() -> Dict[str, Any]:
    """Profiles from the pre-1.2 plugin-directory ``config.yaml``.

    Kept as a read-only fallback: that file is inside the installed plugin snapshot,
    so ``hermes plugins install --force`` wipes it. New config belongs in the Hermes
    profile's own config.yaml under ``plugins.entries``.
    """
    path = Path(__file__).parent / "config.yaml"
    if not path.is_file():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.warning("could not read legacy %s: %s", path, e)
        return {}
    profiles = data.get("profiles")
    return profiles if isinstance(profiles, dict) else {}


def _profiles() -> Dict[str, Dict[str, Any]]:
    """Configured profiles, read fresh on every call (edit config.yaml, no restart)."""
    configured = _ctx.get_config("profiles", None) if _ctx is not None else None
    if not isinstance(configured, dict) or not configured:
        configured = _legacy_file_profiles()
    return {str(k): v for k, v in configured.items() if isinstance(v, dict)}


def _default_data_dir(name: str) -> str:
    """Per-profile user-data-dir under HERMES_HOME. Deliberately NOT a browser's own
    default dir: Chromium >=136 refuses remote debugging there."""
    return str(_hermes_home() / "browser-profiles" / name)


def _resolve(name: str, cfg: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Normalize one profile entry into a launch/connect spec, or return an error string.

    ``host`` present => remote (never launched). Otherwise local: ``browser`` defaults
    to the system default Chromium, ``data_dir`` to a dedicated dir under HERMES_HOME.
    """
    bc = _host()
    port = cfg.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        return None, f"profile '{name}': 'port' must be an integer 1-65535 (got {port!r})"

    host = str(cfg.get("host") or "").strip()
    if host:
        return {"name": name, "remote": True, "host": host, "port": port,
                "url": f"http://{host}:{port}"}, None

    browser = str(cfg.get("browser") or cfg.get("browser_type") or "").strip().lower()
    if browser in ("", "auto"):
        browser = bc.detect_default_chromium() or "chrome"
    if browser == bc.UNSUPPORTED_CHANNEL:
        return None, (f"profile '{name}': your default browser is a pre-release Chromium channel "
                      "(Beta/Dev/Canary), which has no resolvable profile path. Set 'browser' "
                      "explicitly to chrome, chromium, brave, brave-origin or edge.")
    known = list(bc._BROWSER_BY_KEY)
    if browser not in known:
        return None, f"profile '{name}': unknown browser {browser!r} (known: {', '.join(known)})"

    binary = cfg.get("binary") or bc.chromium_executable(browser)
    if binary:
        binary = os.path.expanduser(str(binary))
    if not binary or not os.path.isfile(binary):
        return None, (f"profile '{name}': no executable found for '{browser}'"
                      + (f" at {binary}" if binary else "")
                      + ". Install it, or set 'binary' to its absolute path.")

    data_dir = os.path.expanduser(str(cfg.get("data_dir") or "")) or _default_data_dir(name)
    if (clash := _default_dir_clash(data_dir)):
        return None, f"profile '{name}': {clash}"

    return {"name": name, "remote": False, "browser": browser, "binary": binary,
            "port": port, "data_dir": data_dir,
            "profile_directory": str(cfg.get("profile_directory") or "").strip(),
            "headless": _headless(cfg), "url": None}, None


def _default_dir_clash(data_dir: str) -> Optional[str]:
    """Refuse a data_dir that IS a browser's own default profile dir.

    Chromium >=136 (Google builds) rejects remote debugging on the default
    user-data-dir, and a running browser holds it via SingletonLock — the launch
    would fail or, worse, silently attach to nothing. Hermes already solves
    "browse as my real logged-in self" by snapshotting that profile.
    """
    bc = _host()
    target = os.path.realpath(data_dir)
    for key in bc._BROWSER_BY_KEY:
        real = bc.real_profile_data_dir(key)
        if real and os.path.realpath(real) == target:
            return (f"{data_dir} is {key}'s own default profile directory. Chromium >=136 refuses "
                    "remote debugging there and your running browser holds the lock. To browse as "
                    "your real logged-in self, use Hermes' built-in real-profile support instead: "
                    "set browser.use_real_profile: true in config.yaml (it snapshots the profile and "
                    "launches on the copy). For an isolated profile, drop 'data_dir' and this plugin "
                    "will use a dedicated directory.")
    return None


def _holder_of(data_dir: str) -> Optional[str]:
    """Describe a running Chromium already holding ``data_dir``, else None.

    Chromium's single-instance lock SILENTLY ABSORBS a second launch against a
    user-data-dir some process already owns: the new ``--remote-debugging-port`` is
    ignored, a tab opens in the existing process, and our launch just times out with
    nothing to point at. ``--profile-directory`` does not escape this — the lock is per
    user-data-dir, not per profile. Detected up front so the error is actionable.
    Reported by github.com/LaansDole.
    """
    try:
        procs = list(_host()._processes_holding_profile(data_dir))
    except Exception as e:  # private host helper + optional psutil: never block a launch
        logger.debug("could not probe holders of %s: %s", data_dir, e)
        return None
    for proc in procs:
        try:
            return f"pid {proc.pid} ({proc.name()})"
        except Exception:
            continue
    return None


def _headless(cfg: Dict[str, Any]) -> bool:
    """Whether to add ``--headless=new``. Explicit per-profile ``headless`` wins;
    otherwise headed, except on a Linux host with no display where a headed launch dies."""
    if "headless" in cfg:
        return bool(cfg["headless"])
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return sys.platform.startswith("linux") and not has_display


# --------------------------------------------------------------------------- #
# launch
# --------------------------------------------------------------------------- #

def _lock(name: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(name, threading.Lock())


def _launch_timeout() -> float:
    try:
        return max(1.0, float(_ctx.get_config("launch_timeout", 20) if _ctx else 20))
    except (TypeError, ValueError):
        return 20.0


def _launch(spec: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Launch the profile's browser and wait for CDP. Returns ``(cdp_url, error)``."""
    bc = _host()
    name, port, data_dir = spec["name"], spec["port"], spec["data_dir"]
    # Chromium's single-instance lock is per user-data-dir: a launch against a dir another
    # process holds is absorbed silently (our port never binds). Fail fast and actionably
    # instead of burning the whole launch timeout on a browser that will never appear.
    if (holder := _holder_of(data_dir)):
        return None, (f"a browser is already running on {data_dir} ({holder}), and Chromium's "
                      "single-instance lock would silently swallow this launch — the debug port "
                      "would never bind. Quit that browser, or give this profile its own "
                      "'data_dir'. Note --profile-directory does NOT avoid this: the lock covers "
                      "the whole user-data-dir, not one profile inside it.")
    os.makedirs(data_dir, exist_ok=True)

    argv = [spec["binary"], f"--user-data-dir={data_dir}",
            f"--remote-debugging-port={port}", *_LAUNCH_FLAGS]
    # Edge (and Chrome) REQUIRE --user-data-dir for remote debugging; --profile-directory
    # alone makes Edge >=150 refuse to bind the port. It only selects a profile INSIDE the dir.
    if spec["profile_directory"]:
        argv.append(f"--profile-directory={spec['profile_directory']}")
    if spec["headless"]:
        argv.append("--headless=new")

    log_path = os.path.join(data_dir, "hermes-launch.log")
    try:
        with open(log_path, "w", encoding="utf-8") as log:
            proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=log,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"could not launch {spec['browser']} for '{name}': {e}"
    _launched[name] = proc
    logger.info("browser-profiles: launched %s for '%s' on port %d (%s)",
                spec["browser"], name, port, data_dir)

    deadline = time.monotonic() + _launch_timeout()
    while time.monotonic() < deadline:
        if (url := bc.discover_local_cdp_url(port, timeout=1.0)):
            return url, None
        if proc.poll() is not None:
            return None, (f"{spec['browser']} exited during startup for '{name}': "
                          f"{_log_tail(log_path)}")
        time.sleep(0.25)
    return None, (f"{spec['browser']} did not expose CDP on port {port} for '{name}' within "
                  f"{_launch_timeout():.0f}s: {_log_tail(log_path)}")


def _log_tail(path: str, lines: int = 3) -> str:
    try:
        tail = Path(path).read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return "(no launch log)"
    return " | ".join(tail[-lines:]) or "(empty launch log)"


def _terminate_launched() -> None:
    """Terminate every browser this plugin launched (unload / process exit)."""
    for name, proc in list(_launched.items()):
        _launched.pop(name, None)
        if proc.poll() is not None:
            continue
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except OSError as e:
            logger.debug("could not terminate browser for '%s': %s", name, e)


# --------------------------------------------------------------------------- #
# tool
# --------------------------------------------------------------------------- #

def _reachable(spec: Dict[str, Any]) -> Optional[str]:
    """Live CDP URL for a spec, or None."""
    bc = _host()
    if spec["remote"]:
        url = spec["url"]
        return url if bc.is_browser_debug_ready(url, timeout=1.0) else None
    return bc.discover_local_cdp_url(spec["port"], timeout=1.0)


def _listing() -> Dict[str, Any]:
    global _active
    entries = []
    for name, cfg in _profiles().items():
        spec, err = _resolve(name, cfg)
        if spec is None:
            entries.append({"name": name, "error": err})
            continue
        entries.append({
            "name": name,
            "kind": "remote" if spec["remote"] else "local",
            "port": spec["port"],
            "reachable": _reachable(spec) is not None,
            "active": name == _active,
            **({"host": spec["host"]} if spec["remote"]
               else {"browser": spec["browser"], "data_dir": spec["data_dir"]}),
        })
    if _active and not any(e.get("active") and e.get("reachable") for e in entries):
        _active = None
    return {"profiles": entries, "active": _active,
            "cdp_url": os.environ.get("BROWSER_CDP_URL", ""),
            "hint": ("No profiles configured. Add them under plugins.entries.chrome-profiles."
                     "settings.profiles in config.yaml — see the plugin README.")
            if not entries else None}


def _connect(url: str, name: Optional[str]) -> None:
    """Point every browser surface at ``url`` (or release it when ``url`` is empty)."""
    global _active
    if url:
        os.environ["BROWSER_CDP_URL"] = url
    else:
        os.environ.pop("BROWSER_CDP_URL", None)
    _flush_sessions()
    _supervisor(start=bool(url))
    _active = name


def browser_profile(args: Dict[str, Any], **kwargs) -> str:
    """Switch the agent's browser tools to a named Chromium instance via CDP."""
    try:
        return _browser_profile(args or {})
    except Exception as e:  # handlers must never raise into tool dispatch
        logger.exception("browser_profile failed")
        return json.dumps({"error": f"browser_profile failed: {e}"})


def _browser_profile(args: Dict[str, Any]) -> str:
    name = str(args.get("name") or "").strip()

    if args.get("disconnect"):
        if not os.environ.get("BROWSER_CDP_URL", "").strip():
            return json.dumps({"success": True, "message": "No profile was active."})
        _connect("", None)
        return json.dumps({"success": True, "message": (
            "Released the CDP override. Browser tools reverted to the configured backend "
            "(browser.backend / cloud provider / local Chromium).")})

    if not name:
        return json.dumps(_listing())

    profiles = _profiles()
    if name not in profiles:
        return json.dumps({"error": f"unknown profile {name!r}",
                           "available": sorted(profiles) or None})

    spec, err = _resolve(name, profiles[name])
    if spec is None:
        return json.dumps({"error": err, "profile": name})

    bc = _host()
    with _lock(name):
        url = _reachable(spec)
        if url is None and spec["remote"]:
            return json.dumps({"error": f"remote profile '{name}' is not answering CDP at "
                                        f"{spec['host']}:{spec['port']}", "profile": name})
        if url is None:
            if bc.local_port_in_use(spec["port"], timeout=0.5):
                return json.dumps({
                    "error": (f"port {spec['port']} for '{name}' is held by something that is not a "
                              "CDP endpoint (another app, or a browser started without "
                              "--remote-debugging-port). Free the port or pick another."),
                    "profile": name})
            url, err = _launch(spec)
            if url is None:
                return json.dumps({"error": err, "profile": name, "port": spec["port"]})
        _connect(url, name)

    return json.dumps({
        "success": True, "profile": name, "cdp_url": url,
        "kind": "remote" if spec["remote"] else "local",
        "launched": name in _launched,
        "message": (f"Browser tools now drive '{name}' at {url}. This browser is user-visible and "
                    "may hold logged-in sessions — avoid closing tabs or navigating away from work "
                    "the user cares about."),
    })


SCHEMA = {
    "name": "browser_profile",
    "description": (
        "Switch the agent's browser tools to a named Chromium-family browser over CDP, so they "
        "inherit that browser's cookies and logged-in sessions. Affects every browser surface: "
        "browser_exec (Browser Use CLI), the built-in browser_navigate/click/snapshot tools, and "
        "browser_vault_*. Local profiles are launched on demand; remote profiles must already be "
        "listening. Call with no arguments to list profiles and see which one is active. Call with "
        "disconnect=true to release the override and go back to the configured backend."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Profile name to switch to. Omit to list profiles."},
            "disconnect": {"type": "boolean", "description": "Release the CDP override instead of switching."},
        },
        "required": [],
    },
}


def _slash(raw_args: str) -> str:
    """``/browser-profile [name|off]`` — same logic, human-readable output."""
    arg = (raw_args or "").strip()
    if arg in ("off", "disconnect", "none"):
        payload = json.loads(_browser_profile({"disconnect": True}))
    else:
        payload = json.loads(_browser_profile({"name": arg}))
    if "profiles" in payload:
        if not payload["profiles"]:
            return payload.get("hint") or "No browser profiles configured."
        lines = [f"Active: {payload['active'] or '(none)'}"]
        for entry in payload["profiles"]:
            if "error" in entry:
                lines.append(f"  ✗ {entry['name']} — {entry['error']}")
                continue
            mark = "●" if entry["active"] else ("○" if entry["reachable"] else "·")
            where = entry.get("host") or entry.get("browser")
            lines.append(f"  {mark} {entry['name']} — {entry['kind']} {where} :{entry['port']}")
        return "\n".join(lines)
    return payload.get("message") or payload.get("error") or json.dumps(payload)


def register(ctx):
    """Hermes plugin entry point."""
    global _ctx
    _ctx = ctx
    ctx.register_tool(
        name="browser_profile",
        toolset="chrome_profiles",
        schema=SCHEMA,
        handler=browser_profile,
        description="Switch browser tools to a named Chromium profile via CDP",
        emoji="🔀",
    )
    ctx.register_command("browser-profile", handler=_slash, args_hint="[name|off]",
                         description="Switch browser tools to a named Chromium profile via CDP.")
    # on_unload also covers plugin reload, which a bare atexit handler misses.
    ctx.on_unload(_terminate_launched)
    logger.info("browser-profiles loaded (%d profile(s) configured)", len(_profiles()))


# --------------------------------------------------------------------------- #
# self-check: python __init__.py
# --------------------------------------------------------------------------- #

def _demo() -> None:
    """Exercise config resolution against the real host helpers (launches nothing)."""
    bc = _host()

    ok, err = _resolve("remote-box", {"host": "10.0.0.5", "port": 9222})
    assert err is None and ok["remote"] and ok["url"] == "http://10.0.0.5:9222", (ok, err)

    for bad in ({"port": 0}, {"port": "9222"}, {"port": True}, {}):
        assert _resolve("x", bad)[0] is None, bad

    assert "unknown browser" in _resolve("x", {"port": 9250, "browser": "firefox"})[1]

    browser = next((k for k in bc._BROWSER_BY_KEY if bc.chromium_executable(k)), None)
    if browser:
        spec, err = _resolve("work", {"port": 9250, "browser": browser})
        assert err is None, err
        assert spec["data_dir"] == _default_data_dir("work"), spec["data_dir"]
        assert spec["binary"] and os.path.isfile(spec["binary"]), spec
        # a browser's own default dir must be refused, whichever browser owns it
        clash = _resolve("boom", {"port": 9250, "browser": browser,
                                  "data_dir": bc.real_profile_data_dir(browser)})
        assert clash[0] is None and "use_real_profile" in clash[1], clash
        # explicit headless beats the display heuristic, both ways
        assert _resolve("h", {"port": 9250, "browser": browser, "headless": True})[0]["headless"]
        assert not _resolve("h", {"port": 9250, "browser": browser, "headless": False})[0]["headless"]
    else:
        print("skip: no Chromium-family browser installed, local-profile checks not run")

    assert _resolve("x", {"port": 9250, "browser": browser or "chrome",
                          "binary": "/nope/chrome"})[0] is None

    # single-instance-lock probe: no holder for a dir nothing runs on, and a real holder
    # is detected by cmdline (spawn a fake process whose argv carries the user-data-dir).
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        assert _holder_of(tmp) is None, "unheld dir reported a holder"
        fake = subprocess.Popen([sys.executable, "-c",
                                 f"import time; time.sleep(30)  # chrome --user-data-dir={tmp}"])
        try:
            # argv0 is python, not a browser binary, so the host helper must NOT match it —
            # this asserts the probe binds on browser identity, not just the path string.
            assert _holder_of(tmp) is None, "probe matched a non-browser process"
        finally:
            fake.terminate()
            fake.wait(timeout=5)

    print("browser-profiles self-check OK")


if __name__ == "__main__":
    _demo()
