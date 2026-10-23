import copy
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('public_bootstrap',ROOT/'bootstrap.py')
bootstrap=importlib.util.module_from_spec(spec);spec.loader.exec_module(bootstrap)


class DeploymentTests(unittest.TestCase):
    def settings(self):
        return dict(api_base='https://api.example.invalid/v1',api_key='test-placeholder',model='test-model',
                    admin_qq='123456789',dashboard_password='TestPasswordOnly123',review_enabled=False)

    def test_configuration_isolated_secure_and_review_optional(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            shutil.copytree(ROOT/'AstrBot/data/plugins',root/'AstrBot/data/plugins')
            target=bootstrap.configure(self.settings(),root=root)
            from astrbot.cli.utils import check_astrbot_root
            self.assertTrue(check_astrbot_root(root/'AstrBot'))
            value=json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(value['dashboard']['host'],'127.0.0.1')
            self.assertTrue(value['dashboard']['pbkdf2_password'].startswith('pbkdf2_sha256$'))
            self.assertNotEqual(value['platform'][0]['ws_reverse_token'],'')
            cfg=json.loads((root/'AstrBot/data/config/astrbot_plugin_proactive_agent_config.json').read_text(encoding='utf-8'))
            self.assertFalse(cfg['reply_review_enabled']);self.assertFalse(cfg['enable'])
            self.assertEqual(cfg['fixed_group_ids'],'')
            bili=json.loads((root/'AstrBot/data/config/astrbot_plugin_bili_scroll_config.json').read_text(encoding='utf-8'))
            self.assertFalse(bili['auto_enabled'])
            self.assertEqual(bili['private_ids'],'')
            before=target.read_bytes()
            with self.assertRaises(FileExistsError): bootstrap.configure(self.settings(),root=root)
            self.assertEqual(target.read_bytes(),before)

    def test_placeholder_and_invalid_configuration_rejected(self):
        for key,val in [('api_key','YOUR_API_KEY'),('admin_qq','bot-account'),('dashboard_password','weak'),('api_base','file:///secret')]:
            settings=self.settings();settings[key]=val
            with self.subTest(key=key), self.assertRaises(ValueError): bootstrap.validate(settings)
