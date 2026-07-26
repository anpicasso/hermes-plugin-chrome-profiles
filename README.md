# Chrome/Brave/Edge Profiles Plugin

**Instructions from Hermes Agent:**
```
╭─ ⚕ Hermes ──────────────────────────────────────────────────────────────────────────────────────────────────────────╮
    Here's exactly how it works going forward:
    
    How it works now:
    - You do NOT need to manually quit Brave / relaunch it with debug flags anymore.
    - In any new Hermes session, just say something like "connect to my Research Brave profile"
    and I'll call browser_profile(name="profile 2").
    - The plugin checks if port 9222 is already listening with CDP ready. If yes, it just attaches instantly, no relaunch.
    - If Brave isn't running at all, it auto-launches `Brave Browser --profile-directory="Profile 3"
    --remote-debugging-port=9222` for you and waits for it to come up.
    - The only manual step that's still needed: if you have a plain, non-debug Brave window already open using the
    default profile/data_dir, that one holds the single-instance lock and blocks the launch — you'd need to quit it
    first. Once Profile 3 is launched in debug mode, it persists and future sessions just reattach.
╰─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Switch the agent's browser tools between multiple Chrome, Brave, **or Microsoft Edge** instances via CDP (Chrome DevTools Protocol). Each profile maps to a browser instance with its own user data directory, cookies, and authenticated sessions.

Supports **Google Chrome** and **Brave** (`--user-data-dir`, optionally combined with `--profile-directory` to select a named sub-profile) and **Microsoft Edge** (`--profile-directory` only).

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

**A second launch against an *already-running* browser's `data_dir` gets
silently absorbed.** If a Chrome/Brave process is already open using a
given `--user-data-dir` (e.g. your everyday default browser), launching
another instance pointed at that *same* directory — even with a different
`--profile-directory` and a fresh `--remote-debugging-port` — gets
silently absorbed by Chromium's single-instance lock. The new port never
comes up (the tool call times out); the request just opens a window/tab
in whatever process was already running, ignoring the new debugging port.

**To manage a named profile (e.g. Chrome/Brave's "Profile 2") living
inside a shared `data_dir`,** you have two options:

1. **Quit the existing browser first**, then set `data_dir` to that
   shared directory and add `profile_directory: "Profile 2"` (Chrome/Brave
   now supports this field, same as Edge). The plugin launches
   `--user-data-dir=<data_dir> --profile-directory=<profile_directory>`
   against the now-idle directory, so the named profile — and its
   existing cookies/logins — comes up directly. This is the fastest path
   and requires no copying, but any other window using that `data_dir`
   must be closed first (and stays closed until you relaunch it
   separately).
2. **Copy the profile out into its own directory** if you need it running
   *alongside* the original browser instance:

   ```
   cp -R "$HOME/Library/Application Support/Google/Chrome/Profile 2" ~/.config/chrome-work
   ```

   Then point a `config.yaml` entry's `data_dir` at the copy (omit
   `profile_directory`). It's a snapshot, not a live link — logins made
   in the original profile afterward won't appear in the copy.

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
| `profile_directory` | string | No | Named sub-profile within `data_dir` (e.g., `Default`, `Profile 2`). **Required** for Edge local profiles. Optional for Chrome/Brave local profiles — when set without `data_dir`, defaults to the browser's standard macOS user-data-dir. See the single-instance-lock caveat above before relying on this for Chrome/Brave. |
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
