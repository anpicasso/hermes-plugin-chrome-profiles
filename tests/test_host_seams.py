"""Regression tests for the host seams this plugin rides on.

Run against the installed Hermes source::

    PYTHONPATH=~/.hermes/hermes-agent python -m unittest discover -s tests -v

Each test pins a seam that exists in the *current* host. The failures these
catch are silent in production: the plugin wraps both calls in try/except (a
stale session is not worth failing a switch over), so a moved module or renamed
helper leaves the switch "successful" while sessions stay stale and the CDP
supervisor never attaches.
"""

import importlib.util
import json
import os
import types
import unittest
from pathlib import Path
from unittest import mock

_PLUGIN_PATH = Path(__file__).resolve().parents[1] / "__init__.py"
_spec = importlib.util.spec_from_file_location("chrome_profiles_plugin", _PLUGIN_PATH)
assert _spec and _spec.loader
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)


class HostSeams(unittest.TestCase):
    def test_flush_uses_profile_scoped_cleanup(self):
        from hermes_constants import hermes_home_key

        with mock.patch("tools.browser_session_lifecycle.cleanup_profile_browsers") as cleanup:
            plugin._flush_sessions()
        cleanup.assert_called_once_with(hermes_home_key())

    def test_supervisor_helpers_are_current_names(self):
        with mock.patch("tools.browser_session_cdp.ensure_cdp_supervisor") as start, \
                mock.patch("tools.browser_session_cdp.stop_cdp_supervisor") as stop:
            plugin._supervisor(True)
            plugin._supervisor(False)
        start.assert_called_once_with("default")
        stop.assert_called_once_with("default")

    def test_connect_publishes_live_override_and_disconnect_clears_it(self):
        from tools.browser_session_cdp import (get_cdp_override_raw, live_cdp_override,
                                               set_live_cdp_override)
        url = "http://127.0.0.1:59998"
        set_live_cdp_override(None)
        with mock.patch.object(plugin, "_flush_sessions"), mock.patch.object(plugin, "_supervisor"):
            plugin._connect(url)
        try:
            self.assertEqual(live_cdp_override(), url)
            self.assertEqual(get_cdp_override_raw(), url)
            self.assertEqual(plugin._current_override(), url)
            payload = json.loads(plugin._browser_profile({"disconnect": True}))
            self.assertTrue(payload["success"], payload)
            self.assertEqual(live_cdp_override(), "")
        finally:
            set_live_cdp_override(None)

    def test_cli_publishes_env_only_so_builtin_disconnect_can_clear_it(self):
        from tools.browser_session_cdp import live_cdp_override, set_live_cdp_override
        url = "http://127.0.0.1:59997"
        saved_ctx = plugin._ctx
        setattr(plugin, "_ctx", types.SimpleNamespace(_manager=types.SimpleNamespace(_cli_ref=object())))
        set_live_cdp_override(None)
        try:
            with mock.patch.object(plugin, "_flush_sessions"), mock.patch.object(plugin, "_supervisor"):
                plugin._connect(url)
            self.assertEqual(os.environ.get("BROWSER_CDP_URL"), url)
            self.assertEqual(live_cdp_override(), "")
        finally:
            setattr(plugin, "_ctx", saved_ctx)
            os.environ.pop("BROWSER_CDP_URL", None)
            set_live_cdp_override(None)

    def test_listing_tolerates_a_broken_profile_entry(self):
        with mock.patch.object(plugin, "_profiles",
                               return_value={"broken": {"port": 0}, "probe": {"port": 9382}}), \
                mock.patch.object(plugin, "_reachable", return_value=None), \
                mock.patch.object(plugin, "_current_override", return_value=""):
            listing = plugin._listing()
        self.assertIsNone(listing["active"])
        self.assertIn("error", listing["profiles"][0])

    def test_listing_marks_the_live_override_active(self):
        url = "http://127.0.0.1:9381"
        with mock.patch.object(plugin, "_profiles", return_value={"probe": {"port": 9381}}), \
                mock.patch.object(plugin, "_reachable", return_value=url), \
                mock.patch.object(plugin, "_current_override", return_value=url):
            listing = plugin._listing()
        self.assertEqual(listing["active"], "probe")
        self.assertTrue(listing["profiles"][0]["active"])
        self.assertEqual(listing["cdp_url"], url)


if __name__ == "__main__":
    unittest.main()
