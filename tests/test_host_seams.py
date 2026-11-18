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
import tempfile
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


class ContributionRegressions(unittest.TestCase):
    """Pin the v2 equivalents of the closed v1 fork PRs (#2, #3, #4).

    These are regressions, not new behavior: each assertion describes a seam that
    already exists in v2.0.1. They exist so a future rewrite cannot silently drop
    the capability a fork asked for while the original patch stays unmerged.
    """

    def test_host_browser_registry_keys_resolve(self):
        """PR #2: Brave/Brave Origin resolve through the host registry, not a v1 table."""
        bc = plugin._host()
        with tempfile.TemporaryDirectory() as tmp:
            binary = os.path.join(tmp, "browser")
            Path(binary).write_text("", encoding="utf-8")
            with mock.patch.object(bc, "chromium_executable", return_value=binary):
                for key in bc._BROWSER_BY_KEY:
                    spec, err = plugin._resolve(
                        key, {"port": 9250, "browser": key,
                              "data_dir": os.path.join(tmp, key)})
                    self.assertIsNone(err, (key, err))
                    self.assertEqual(spec["browser"], key)
                    self.assertEqual(spec["binary"], binary)

    def test_browser_default_data_dir_is_refused(self):
        """PR #2 follow-up: the unsafe default-dir fallback stays rejected."""
        bc = plugin._host()
        with tempfile.TemporaryDirectory() as tmp:
            binary = os.path.join(tmp, "browser")
            Path(binary).write_text("", encoding="utf-8")
            default_dir = bc.real_profile_data_dir("brave")
            self.assertIsNotNone(default_dir)
            with mock.patch.object(bc, "chromium_executable", return_value=binary):
                spec, err = plugin._resolve(
                    "brave", {"port": 9250, "browser": "brave", "data_dir": default_dir})
        self.assertIsNone(spec)
        self.assertIn("use_real_profile", err)

    def test_launch_creates_data_dir_and_passes_profile_directory(self):
        """PR #2: first launch can open its log; --profile-directory is appended."""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "profile")
            spec = {"name": "work", "browser": "brave", "binary": "/fake/brave",
                    "port": 9250, "data_dir": data_dir,
                    "profile_directory": "Profile 2", "headless": False}
            proc = mock.Mock()
            proc.poll.return_value = 0
            try:
                with mock.patch.object(plugin, "_holder_of", return_value=None), \
                        mock.patch.object(plugin, "_launch_timeout", return_value=0.0), \
                        mock.patch("subprocess.Popen", return_value=proc) as popen:
                    url, err = plugin._launch(spec)
            finally:
                plugin._launched.pop("work", None)
            argv = popen.call_args.args[0]
            self.assertTrue(os.path.isdir(data_dir))
            self.assertTrue(os.path.isfile(os.path.join(data_dir, "hermes-launch.log")))
            self.assertIn(f"--user-data-dir={data_dir}", argv)
            self.assertIn(f"--remote-debugging-port={spec['port']}", argv)
            self.assertIn("--profile-directory=Profile 2", argv)
            self.assertIsNone(url)
            self.assertIn("did not expose CDP", err)

    def test_launch_timeout_defaults_to_documented_20_seconds(self):
        """PR #3: the README documents 20; keep the code and bad-config fallback at 20."""
        saved = plugin._ctx
        try:
            setattr(plugin, "_ctx", None)
            self.assertEqual(plugin._launch_timeout(), 20.0)
            setattr(plugin, "_ctx",
                    types.SimpleNamespace(get_config=lambda key, default=None: "bad"))
            self.assertEqual(plugin._launch_timeout(), 20.0)
            setattr(plugin, "_ctx",
                    types.SimpleNamespace(get_config=lambda key, default=None: 5))
            self.assertEqual(plugin._launch_timeout(), 5.0)
        finally:
            setattr(plugin, "_ctx", saved)


if __name__ == "__main__":
    unittest.main()
