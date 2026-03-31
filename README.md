# Chrome/Edge Profiles Plugin

Switch the agent's browser tools between multiple Chrome **or Microsoft Edge** instances via CDP (Chrome DevTools Protocol). Each profile maps to a browser instance with its own user data directory, cookies, and authenticated sessions.

Supports both **Google Chrome** (`--user-data-dir`) and **Microsoft Edge** (`--profile-directory`).

## How it works

The plugin registers a single tool: `browser_profile(name)`.

When called, it:

1. Looks up the named profile in `config.yaml`
2. **Local profiles** — checks if browser is running on the configured port. If not, launches it with the correct arguments (`--user-data-dir` for Chrome, `--profile-directory` for Edge) and `--remote-debugging-port`. Waits for the port to come up.
3. **Remote profiles** — checks if `host:port` is reachable. Fails immediately if not (no launch attempt).
4. Sets `BROWSER_CDP_URL` in the running process so all subsequent browser tool calls (`browser_navigate`, `browser_click`, `browser_snapshot`, `browser_vision`, etc.) go through that browser instance.
5. Flushes any stale browser sessions from previous connections.

Calling `browser_profile()` with no arguments lists all available profiles and which one is currently active.

## Configuration

Copy the example config and edit it:

```
cp config.yaml.example config.yaml
```

Then edit `config.yaml` to match your setup:

```yaml
# Optional: global Chrome binary path (overrides PATH auto-detection)
# chrome_binary: /opt/google/chrome/google-chrome

profiles:
  work:
    type: local
    port: 9250
    data_dir: ~/.config/chrome-work

  personal:
    type: local
    port: 9251
    data_dir: ~/.config/chrome-personal
    # Optional: per-profile Chrome binary override
    # chrome_binary: /usr/bin/google-chrome-beta

  # Microsoft Edge profile (no data_dir, uses profile_directory)
  devsu:
    type: local
    browser_type: edge
    port: 9223
    profile_directory: "Default"

  remote-server:
    type: remote
    host: 192.168.1.100
    port: 9250
```

### Profile fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | `local` or `remote` | Yes | Local profiles can auto-launch browser. Remote profiles only check reachability. |
| `port` | integer | Yes | Browser remote debugging port. |
| `browser_type` | `chrome`, `edge`, or `auto` | No | Browser type. `auto` tries Chrome then Edge. Default: `auto`. |
| `data_dir` | string | Chrome local | Path to Chrome user data directory. Supports `~` expansion. |
| `profile_directory` | string | Edge local | Edge profile name (e.g., `Default`). Required for Edge profiles. |
| `host` | string | Remote only | Hostname or IP of the remote browser instance. |
| `chrome_binary` | string | No | Absolute path to Chrome executable. Overrides global and PATH detection. |
| `edge_binary` | string | No | Absolute path to Edge executable. Overrides global and PATH detection. |

### Browser binary resolution (local profiles)

When a local profile needs to launch a browser, the binary is resolved based on `browser_type`:

**For Chrome profiles:**
1. Profile-level `chrome_binary` field
2. Top-level `chrome_binary` field
3. Auto-detect from PATH: `google-chrome`, `google-chrome-stable`, `google-chrome-beta`, `chromium-browser`, `chromium`

**For Edge profiles:**
1. Profile-level `edge_binary` field
2. Top-level `edge_binary` field
3. Auto-detect from PATH: `microsoft-edge`, `microsoft-edge-stable`, `edge`

**For `browser_type: auto`:** Tries Chrome first, then Edge.

### Config reloading

`config.yaml` is re-read on every `browser_profile()` call. Edit the file and the next call picks up changes — no restart needed.

## Usage examples

From the agent's perspective (these are tool calls the LLM makes):

```
# List all profiles
browser_profile()

# Switch to work profile (auto-launches Chrome if needed)
browser_profile(name="work")

# Now all browser tools use that Chrome instance:
browser_navigate(url="https://mail.google.com")
browser_snapshot()

# Switch to a different profile
browser_profile(name="personal")
browser_navigate(url="https://github.com")

# Connect to Chrome on another machine
browser_profile(name="remote-server")
```

## Installation

Drop this directory into `~/.hermes/plugins/` or install with:

```
hermes plugins install anpicasso/hermes-plugin-chrome-profiles
```

The installer will automatically copy `config.yaml.example` to `config.yaml` for you.

## Files

```
~/.hermes/plugins/chrome-profiles/
├── plugin.yaml          # Plugin manifest
├── config.yaml.example  # Configuration template
├── config.yaml          # Your configuration (created on install, gitignored)
├── __init__.py          # Tool registration and logic
└── README.md            # This file
```
