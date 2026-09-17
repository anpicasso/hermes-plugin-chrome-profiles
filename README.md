# hermes-plugin-chrome-profiles

Switch Hermes' browser tools to a named Chromium-family browser over CDP, so the agent
inherits that browser's cookies and logged-in sessions.

Adds one tool, `browser_profile`, and one slash command, `/browser-profile`.

## What it affects

Setting a profile sets `BROWSER_CDP_URL` — the same public override `/browser connect`
uses — which sits at the top of Hermes' backend precedence. So a switch redirects
**every** browser surface at once:

- `browser_exec` (Browser Use CLI backend)
- the built-in `browser_navigate` / `browser_click` / `browser_snapshot` / `browser_vision` tools
- `browser_vault_fill` and friends (the CDP supervisor is attached for you)

It also flushes live browser sessions, so the next tool call dials the new endpoint
instead of a stale one.

## Requirements

- A Chromium-family browser: Chrome, Chromium, Brave, Brave Origin, or Edge.
  Firefox and Camofox have no CDP surface and are not supported.
- `browser.backend: off` is fine, `browser-use` is fine, a cloud provider is fine — the
  override wins over all of them. `browser.cdp_url` in config.yaml is the one thing that
  outranks the plugin for tools that read config directly, so leave it unset.

## Configuration

Profiles live in your Hermes profile's `config.yaml`, not in the plugin directory
(a reinstall replaces the plugin snapshot):

```yaml
plugins:
  entries:
    chrome-profiles:
      settings:
        launch_timeout: 20        # optional, seconds
        profiles:
          work:
            port: 9250           # required

          uni:
            port: 9251
            browser: edge        # chrome | chromium | brave | brave-origin | edge
            headless: false

          scraper:
            port: 9252
            headless: true

          box:
            host: 192.168.1.100  # any 'host' => remote, never launched
            port: 9222
```

Edits are picked up on the next call — no restart.

### Profile fields

| Field | Required | Meaning |
|---|---|---|
| `port` | yes | Remote-debugging port. |
| `host` | — | Set it to make the profile **remote**: reachability is checked, nothing is launched. |
| `browser` | — | Which Chromium family binary. Default: your system default Chromium. |
| `binary` | — | Absolute path, overriding `browser` detection. |
| `data_dir` | — | User-data-dir. Default: `~/.hermes/browser-profiles/<name>`. |
| `profile_directory` | — | Profile *inside* the data dir (`Default`, `Profile 2`). |
| `headless` | — | Default: headed, except on a display-less Linux host where a headed launch would die. |

### `data_dir` and your everyday browser

Pointing `data_dir` at a browser's own default profile is **refused**. Chromium ≥136
rejects remote debugging on the default user-data-dir, and your running browser holds it
via `SingletonLock` — the launch fails or attaches to nothing.

To browse as your real logged-in self, use Hermes' built-in support instead, which
snapshots the profile and launches on the copy:

```yaml
browser:
  use_real_profile: true
```

This plugin is for *dedicated* profiles: separate work/personal/scraper identities you
log into once, plus remote browsers on other machines.

### One browser per `data_dir`

Chromium's single-instance lock is per user-data-dir. Launching a second browser against
a directory another process already holds is **silently absorbed** — the new
`--remote-debugging-port` is ignored, a tab just opens in the existing process, and the
port never binds. `--profile-directory` does not escape this; the lock covers the whole
user-data-dir, not one profile inside it.

The plugin detects the holder before launching and fails immediately, naming the PID,
instead of burning `launch_timeout` on a browser that will never appear. Give each
profile its own `data_dir` (the default already does), or quit the holder first.

*This failure mode was diagnosed by [LaansDole](https://github.com/LaansDole).*

## Usage

```
browser_profile()                        # list profiles, show which is active
browser_profile(name="work")             # switch (launches on demand)
browser_profile(disconnect=True)         # release the override
```

```
/browser-profile          # list
/browser-profile work     # switch
/browser-profile off      # release
```

## Installation

```
hermes plugins install anpicasso/hermes-plugin-chrome-profiles
```

Then add a `profiles` block to `config.yaml` (see above) and restart the gateway —
Python plugins have no hot reload.

## Notes

- Browsers this plugin launched are terminated on plugin unload / reload. A browser that
  was already listening on the port is left alone.
- A port held by a non-CDP process is reported as an error rather than fought over.
- The launched browser is user-visible and may hold live logins; the tool result says so,
  so the agent avoids closing tabs or navigating away from the user's work.

## Credits

The single-instance-lock failure mode (a second launch against a held `data_dir` being
silently absorbed) was diagnosed by [LaansDole](https://github.com/LaansDole/hermes-plugin-chrome-profiles).

## Self-check

```
PYTHONPATH=~/.hermes/hermes-agent python3 ~/.hermes/plugins/chrome-profiles/__init__.py
```

Exercises config resolution against the real host helpers. Launches nothing.
