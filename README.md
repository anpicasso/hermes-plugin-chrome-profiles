# Chrome Profiles Plugin

Switch the agent's browser tools between multiple Chrome instances via CDP (Chrome DevTools Protocol). Each profile maps to a Chrome instance with its own user data directory, cookies, and authenticated sessions.

## How it works

The plugin registers a single tool: `browser_profile(name)`.

When called, it:

1. Looks up the named profile in `config.yaml`
2. **Local profiles** — checks if Chrome is running on the configured port. If not, launches it with the correct `--user-data-dir` and `--remote-debugging-port`. Waits for the port to come up.
3. **Remote profiles** — checks if `host:port` is reachable. Fails immediately if not (no launch attempt).
4. Sets `BROWSER_CDP_URL` in the running process so all subsequent browser tool calls (`browser_navigate`, `browser_click`, `browser_snapshot`, `browser_vision`, etc.) go through that Chrome instance.
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

  remote-server:
    type: remote
    host: 192.168.1.100
    port: 9250
```

### Profile fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | `local` or `remote` | Yes | Local profiles can auto-launch Chrome. Remote profiles only check reachability. |
| `port` | integer | Yes | Chrome remote debugging port. |
| `data_dir` | string | Local only | Path to Chrome user data directory. Supports `~` expansion. |
| `host` | string | Remote only | Hostname or IP of the remote Chrome instance. |
| `chrome_binary` | string | No | Absolute path to Chrome executable. Overrides global and PATH detection. |

### Chrome binary resolution (local profiles)

When a local profile needs to launch Chrome, the binary is resolved in this order:

1. Profile-level `chrome_binary` field
2. Top-level `chrome_binary` field
3. Auto-detect from PATH: `google-chrome`, `google-chrome-stable`, `google-chrome-beta`, `chromium-browser`, `chromium`

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
