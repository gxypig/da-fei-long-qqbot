import unittest,shutil,subprocess
from pathlib import Path
from types import SimpleNamespace
from test_maintenance import ROOT
from quart import Quart
from data.plugins.astrbot_plugin_proactive_agent.panel_subview import bundled_view,register_subview_panel

class SubviewTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(shutil.which('node'),'Node.js needed for page bridge test')
    def test_reconnect_keeps_draft_and_does_not_hide_write_conflict(self):
        run=subprocess.run([shutil.which('node'),str(ROOT/'tests/persona_page_bridge.cjs'),
            str(ROOT/'AstrBot/data/plugins/astrbot_plugin_proactive_agent/pages/persona/app.js')],
            capture_output=True,text=True,encoding='utf-8',timeout=15)
        self.assertEqual(run.returncode,0,run.stdout+run.stderr)

    async def test_only_bundled_views_no_arbitrary_paths(self):
        for path in ('../config','control','/etc/passwd','sessions/../persona','',None):
            with self.assertRaises(ValueError):bundled_view(path)
        for name in ('persona','sessions'):
            html=bundled_view(name)
            self.assertIn('<style>',html)
            self.assertIn('<script>',html)
            self.assertNotIn('src="./',html)
            self.assertNotIn('bridge-sdk.js',html)

    async def test_route_uses_existing_api_registration(self):
        app=Quart(__name__)
        def register(path,handler,methods,description):app.add_url_rule(path,view_func=handler,methods=methods)
        register_subview_panel(SimpleNamespace(context=SimpleNamespace(register_web_api=register)))
        client=app.test_client()
        r=await client.get('/astrbot_plugin_proactive_agent/panel_subview?view=persona')
        d=await r.get_json();self.assertEqual(d['data']['endpoints'],['persona_state','persona_action'])
        self.assertEqual((await client.get('/astrbot_plugin_proactive_agent/panel_subview?view=../')).status_code,400)

    def test_host_keeps_sandbox_and_endpoint_allowlist(self):
        text=(ROOT/'AstrBot/data/plugins/astrbot_plugin_proactive_agent/pages/control/subview.js').read_text('utf-8')
        self.assertNotIn('allow-same-origin',text)
        self.assertNotIn('allow-popups',text)
        self.assertIn('e.source!==frame.contentWindow',text)
        self.assertIn('allow[active.view].includes(m.endpoint)',text)
