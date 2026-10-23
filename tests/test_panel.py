import asyncio
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent import panel
from quart import Quart


class Config(dict):
    def save_config(self):
        self.saved = copy.deepcopy(dict(self))


class PanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = object.__new__(Main)
        self.plugin.config = Config({**panel.FIELDS, "model_id": "keep-model", "custom_prompt": "keep-prompt"})
        self.plugin._panel_lock = asyncio.Lock()
        self.app = Quart(__name__)
        self.app.add_url_rule("/settings", view_func=self.plugin.panel_settings_api, methods=["GET", "POST"])
        self.app.add_url_rule("/health", view_func=self.plugin.panel_health_api, methods=["GET", "POST"])
        self.client = self.app.test_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.root_patch = patch.object(panel, "PROJECT", Path(self.tmp.name))
        self.root_patch.start()

    async def asyncTearDown(self):
        self.root_patch.stop()
        self.tmp.cleanup()

    async def current(self):
        response = await self.client.get("/settings")
        return (await response.get_json())["data"]

    async def test_save_preserves_existing_functions_and_conflicts_do_not_overwrite(self):
        body = await self.current()
        body["values"].update(group_chat_ids="123，456 123", group_chat_probability=23)
        response = await self.client.post("/settings", json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.plugin.config.saved["group_chat_ids"], "123,456")
        self.assertEqual(self.plugin.config.saved["custom_prompt"], "keep-prompt")
        self.assertEqual(self.plugin.config.saved["model_id"], "keep-model")
        self.plugin.config["group_chat_probability"] = 46  # A command or another tab won.
        response = await self.client.post("/settings", json=body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.plugin.config["group_chat_probability"], 46)

    async def test_invalid_configuration_never_partially_saves(self):
        original = dict(self.plugin.config)
        for key, value in [("group_chat_probability", 101), ("group_chat_probability", True),
                           ("group_chat_ids", "123; invoke-code"), ("quiet_hours", "25:00-06:00"),
                           ("interval_minutes", 0), ("enable", "true")]:
            with self.subTest(key=key, value=value):
                body = await self.current()
                body["values"][key] = value
                response = await self.client.post("/settings", json=body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(dict(self.plugin.config), original)

    async def test_disk_error_restores_live_settings(self):
        body = await self.current()
        body["values"]["allow_private"] = True
        original = dict(self.plugin.config)
        with patch.object(self.plugin.config, "save_config", side_effect=OSError("disk full")):
            response = await self.client.post("/settings", json=body)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(dict(self.plugin.config), original)

    async def test_health_is_read_only_even_if_a_stale_ui_posts(self):
        with patch.object(panel.subprocess, "run", return_value=Mock(returncode=0)) as run:
            response = await self.client.post("/health", json={"mode": "Repair"})
            self.assertEqual(response.status_code, 405)
            self.assertIn("一键检查修复.bat", (await response.get_json())["message"])
        run.assert_not_called()

    async def test_read_only_report_remains_available(self):
        panel.save_job({"status": "running", "started": "2020-01-01T00:00:00+00:00"})
        self.assertEqual(panel.current_job()["status"], "failed")
        response = await self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await response.get_json())["data"]["job"]["status"], "failed")

    async def test_rejects_arbitrary_job_modes_without_spawning(self):
        with patch.object(panel.subprocess, "run") as run:
            for body in ({"mode": "Repair; whoami"}, [], {"mode": "Delete"}):
                response = await self.client.post("/health", json=body)
                self.assertEqual(response.status_code, 405)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
