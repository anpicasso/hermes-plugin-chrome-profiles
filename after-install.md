# Chrome Profiles Plugin — Installed ✓

Manage multiple Chrome browser profiles from your agent's browser tools.

## Configuration

Edit the profiles config at:

```
~/.hermes/plugins/chrome-profiles/config.yaml
```

### Example

```yaml
chrome_binary: /opt/google/chrome/google-chrome   # optional global override

profiles:
  work:
    type: local
    port: 9250
    data_dir: ~/.config/chrome-work

  remote-server:
    type: remote
    host: 192.168.1.100
    port: 9250
```

## Usage

The plugin adds `browser_profile` to the agent's tool list.

| Call | Effect |
|------|--------|
| `browser_profile()` | List profiles + active one |
| `browser_profile(name="work")` | Switch to work (launches Chrome if needed) |

After switching, all browser tools (`browser_navigate`, `browser_click`, etc.) operate on that Chrome instance with full cookie/session access.

## Profile types

- **local** — Auto-launches Chrome if not running. Requires `port` + `data_dir`.
- **remote** — Checks reachability only. Requires `port` + `host`. Fails if unreachable.
