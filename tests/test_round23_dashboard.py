import importlib.util,shutil,subprocess,unittest
from pathlib import Path
from test_maintenance import ROOT

class Dashboard(unittest.TestCase):
    def test_gallery_hung_thumbnail_releases_slot(self):
        node=shutil.which('node')
        if not node and (ROOT/'SnowLuma/node.exe').is_file():node=str(ROOT/'SnowLuma/node.exe')
        if not node:self.skipTest('Node unavailable')
        result=subprocess.run([node,str(ROOT/'tests/round23_gallery.cjs'),str(ROOT/'AstrBot/data/plugins/astrbot_plugin_smart_imagechat_hub/pages/image-center-page/app.js')],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
    def test_console_runtime_flood_lifecycle_and_long_text(self):
        node=shutil.which('node')
        if not node and (ROOT/'SnowLuma/node.exe').is_file():node=str(ROOT/'SnowLuma/node.exe')
        if not node:self.skipTest('Node unavailable')
        result=subprocess.run([node,str(ROOT/'tests/round23_console.cjs'),str(ROOT/'tools/dashboard_console.js')],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
    def test_unknown_dashboard_is_never_overwritten(self):
        spec=importlib.util.spec_from_file_location('console_patch',ROOT/'tools/optimize_dashboard.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with self.assertRaises(ValueError):module.patch_source('unknown build')
