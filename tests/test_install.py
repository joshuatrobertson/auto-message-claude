import json
import os
import unittest

import autoresume
from tests.helpers import TempStateMixin


class TestInstall(TempStateMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.settings = self.tmp / "claude" / "settings.json"
        os.environ["CLAUDE_SETTINGS_PATH"] = str(self.settings)

    def read_settings(self):
        return json.loads(self.settings.read_text())

    def test_fresh_install_wires_both_hooks_and_manifest(self):
        rc = autoresume.cmd_install()
        self.assertEqual(rc, 0)
        data = self.read_settings()
        sf = data["hooks"]["StopFailure"]
        self.assertEqual(sf[0]["matcher"], "rate_limit")
        self.assertIn("hook-stop-failure", sf[0]["hooks"][0]["command"])
        self.assertIn("hook-stop", data["hooks"]["Stop"][0]["hooks"][0]["command"])
        manifest = json.loads(
            (self.state / "install-manifest.json").read_text())
        self.assertEqual(len(manifest["commands"]), 2)
        self.assertEqual(manifest["settings_path"], str(self.settings))

    def test_install_is_idempotent(self):
        autoresume.cmd_install()
        autoresume.cmd_install()
        data = self.read_settings()
        self.assertEqual(len(data["hooks"]["StopFailure"]), 1)
        self.assertEqual(len(data["hooks"]["Stop"]), 1)

    def test_install_preserves_existing_settings_and_backs_up(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({
            "model": "opus",
            "hooks": {"Stop": [{"hooks": [
                {"type": "command", "command": "say done"}]}]},
        }))
        autoresume.cmd_install()
        data = self.read_settings()
        self.assertEqual(data["model"], "opus")
        stop_cmds = [h["command"] for grp in data["hooks"]["Stop"]
                     for h in grp["hooks"]]
        self.assertIn("say done", stop_cmds)
        self.assertEqual(len(stop_cmds), 2)
        backups = list(self.settings.parent.glob("settings.json.backup-*"))
        self.assertEqual(len(backups), 1)

    def test_uninstall_removes_only_ours(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({
            "hooks": {"Stop": [{"hooks": [
                {"type": "command", "command": "say done"}]}]},
        }))
        autoresume.cmd_install()
        rc = autoresume.cmd_uninstall()
        self.assertEqual(rc, 0)
        data = self.read_settings()
        stop_cmds = [h["command"] for grp in data["hooks"]["Stop"]
                     for h in grp["hooks"]]
        self.assertEqual(stop_cmds, ["say done"])
        self.assertNotIn("StopFailure", data["hooks"])
        self.assertFalse((self.state / "install-manifest.json").exists())

    def test_uninstall_without_manifest_says_so(self):
        self.assertEqual(autoresume.cmd_uninstall(), 1)
