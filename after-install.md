# chrome-profiles — installed ✓

Adds `browser_profile` (tool) and `/browser-profile` (slash command).

## 1. Configure profiles in config.yaml

Not in the plugin directory — a reinstall would wipe it.

```yaml
plugins:
  entries:
    chrome-profiles:
      settings:
        profiles:
          work:
            port: 9250
          uni:
            port: 9251
            browser: edge
          box:
            host: 192.168.1.100   # remote: connect only, never launched
            port: 9222
```

Optional per profile: `browser`, `binary`, `data_dir`, `profile_directory`, `headless`.
Defaults: your system default Chromium, headed, `~/.hermes/browser-profiles/<name>`.

## 2. Restart the gateway

Python plugins have no hot reload:

```
hermes gateway restart
```

Profile edits after that are picked up live.

## 3. Use it

```
/browser-profile          list profiles + active one
/browser-profile work     switch (launches the browser if needed)
/browser-profile off      release the override
```

Switching redirects every browser surface — `browser_exec`, the built-in `browser_*`
tools, and `browser_vault_*`.

## Heads up

`data_dir` pointing at a browser's own default profile is refused: Chromium ≥136 blocks
remote debugging there. For "browse as my real logged-in self", set
`browser.use_real_profile: true` in config.yaml instead — Hermes snapshots the profile
and launches on the copy.
