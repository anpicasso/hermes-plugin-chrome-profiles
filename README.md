# Chrome/Brave/Edge Profiles Plugin

Switch the agent's browser tools between multiple Chrome, Brave, **or Microsoft Edge** instances via CDP (Chrome DevTools Protocol). Each profile maps to a browser instance with its own user data directory, cookies, and authenticated sessions.

Supports **Google Chrome** and **Brave** (`--user-data-dir`) and **Microsoft Edge** (`--profile-directory`). Brave is Chromium-based and accepts the same launch flags as Chrome.

## How it works

The plugin registers a single tool: `browser_profile(name)`.

When called, it:

1. Looks up the named profile in `config.yaml`
2. **Local profiles** — checks if browser is running on the configured port. If not, launches it with the correct arguments (`--user-data-dir` for Chrome/Brave, `--profile-directory` for Edge) and `--remote-debugging-port`. Waits for the port to come up.
3. **Remote profiles** — checks if `host:port` is reachable. Fails immediately if not (no launch attempt).
4. Sets `BROWSER_CDP_URL` in the running process so all subsequent browser tool calls (`browser_navigate`, `browser_click`, `browser_snapshot`, `browser_vision`, etc.) go through that browser instance.
5. Flushes any stale browser sessions from previous connections.

Calling `browser_profile()` with no arguments lists all available profiles and which one is currently active.

## What this does NOT do

**`data_dir` (or `profile_directory`) *is* the profile.** This tool does not
reach into an existing browser and select one of its named profiles
(e.g. Chrome/Brave's "Profile 2") — it launches a browser process pointed at
whatever directory you give it. Point it at a directory that already has a
profile, that's what opens.

**It cannot pick a profile out of an already-running shared browser.** If you
normally run one Chrome/Brave with several named profiles (Default,
"Work", "Personal", ...) sharing one `--user-data-dir`, and that browser is
already open, a second launch against that *same* `--user-data-dir` —
even with a different `--profile-directory` and a fresh
`--remote-debugging-port` — gets silently absorbed by Chromium's
single-instance lock. The new port never comes up (the tool call times out);
the request just opens a window/tab in whatever process was already running,
ignoring the new debugging port.

**To manage one of those existing named profiles with this tool**, copy it
out into its own directory first:

```
cp -R "$HOME/Library/Application Support/Google/Chrome/Profile 2" ~/.config/chrome-work
```

Then point a `config.yaml` entry's `data_dir` at the copy. It's a snapshot,
not a live link — logins made in the original profile afterward won't
appear in the copy.

**Switching between two already-launched profiles is instant and safe in
both directions.** `browser_profile()` never stops the browser you're
leaving — it only redirects where the *next* tool call points
(`BROWSER_CDP_URL`). Both processes keep running, so switching back to one
you already opened doesn't relaunch anything.

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

  # Brave profile
  brave:
    type: local
    browser_type: brave
    port: 9252
    data_dir: ~/.config/brave-profile

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
| `browser_type` | `chrome`, `brave`, `edge`, or `auto` | No | Browser type. `auto` tries Chrome, then Brave, then Edge. Default: `auto`. |
| `data_dir` | string | Chrome/Brave local | Path to the browser's user data directory. Supports `~` expansion. |
| `profile_directory` | string | Edge local | Edge profile name (e.g., `Default`). Required for Edge profiles. |
| `host` | string | Remote only | Hostname or IP of the remote browser instance. |
| `chrome_binary` | string | No | Absolute path to the Chrome executable. Overrides global and PATH detection. |
| `brave_binary` | string | No | Absolute path to the Brave executable. Overrides global and PATH detection. |
| `edge_binary` | string | No | Absolute path to the Edge executable. Overrides global and PATH detection. |

### Browser binary resolution (local profiles)

When a local profile needs to launch a browser, the binary is resolved based on `browser_type`. For each type, priority is: profile-level `*_binary` field, top-level `*_binary` field, PATH auto-detect, then (macOS only) the browser's default `/Applications/*.app` path — macOS ships Chrome/Brave/Edge as app bundles with no CLI symlink on PATH, so PATH auto-detect alone never finds them there.

**For Chrome profiles:** `chrome_binary` → PATH (`google-chrome`, `google-chrome-stable`, `google-chrome-beta`, `chromium-browser`, `chromium`) → macOS `Google Chrome.app`.

**For Brave profiles:** `brave_binary` → PATH (`brave-browser`, `brave-browser-stable`, `brave`) → macOS `Brave Browser.app`.

**For Edge profiles:** `edge_binary` → PATH (`microsoft-edge`, `microsoft-edge-stable`, `edge`) → macOS `Microsoft Edge.app`.

**For `browser_type: auto`:** Tries Chrome, then Brave, then Edge.

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
