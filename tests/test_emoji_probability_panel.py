"""Offline API/persistence and page bridge checks; no running bot or model calls."""
import asyncio
import copy
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_maintenance import ROOT  # AstrBot imports use the suite's temporary data directory.
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.dashboard.server import AstrBotDashboard
from data.plugins.astrbot_plugin_proactive_agent import panel
from data.plugins.astrbot_plugin_smart_imagechat_hub.main import SmartImageSenderPlugin
import jwt
from quart import Quart, jsonify, request
from starlette.requests import Request


PLUGIN = ROOT / "AstrBot/data/plugins/astrbot_plugin_proactive_agent"
API = "/api/plug/astrbot_plugin_proactive_agent/emoji_settings"


class EmojiProbabilityAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="emoji-panel-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "gallery.json"
        self.initial = {
            "proactive_emoji_reply": {"enabled": True, "trigger_probability": "0.35",
                "meme_only": True, "retrieval_mode": "bot_reply_serial", "analysis_provider_id": "fixture",
                "embed_in_conversation": True, "debug_mode": False, "custom_nested": {"keep": [1, 2]}},
            "image_caption": {"provider": "fixture-private-value"}, "files": ["fixture.png"],
        }
        self.path.write_text(json.dumps(self.initial), encoding="utf-8")
        self.gallery = object.__new__(SmartImageSenderPlugin)
        self.gallery.config = AstrBotConfig(str(self.path), default_config=copy.deepcopy(self.initial))
        self.gallery._lock = asyncio.Lock()
        self.meta = SimpleNamespace(activated=True, star_cls=self.gallery)
        self.plugin = panel.PanelMixin()
        self.plugin.config = {"emoji_probability": 18, "meme_separate_probability": 50, "other": "keep"}
        self.proactive_before = copy.deepcopy(self.plugin.config)
        self.plugin.context = SimpleNamespace(
            get_registered_star=Mock(side_effect=lambda name: self.meta), register_web_api=Mock())
        self.plugin.register_panel()
        self.app = Quart(__name__)
        for call in self.plugin.context.register_web_api.call_args_list:
            route, handler, methods, _ = call.args
            self.app.add_url_rule("/api/plug" + route, view_func=handler, methods=methods)
        # Exercise the installed dashboard's actual JWT middleware, without starting a server.
        self.dashboard = object.__new__(AstrBotDashboard)
        self.dashboard._jwt_secret = "offline-emoji-panel-fixture-only-not-a-production-secret"
        self.dashboard._apply_auth_rate_limit = AsyncMock(return_value=None)
        self.headers = {"Authorization": "Bearer " + jwt.encode(
            {"username": "offline-test"}, self.dashboard._jwt_secret, algorithm="HS256")}

        @self.app.before_request
        async def authenticate():
            adapted = Request({"type": "http", "method": request.method, "path": request.path,
                "query_string": request.query_string, "headers": [
                    (key.lower().encode(), value.encode()) for key, value in request.headers.items()],
                "state": {"dashboard_g": SimpleNamespace()}})
            response = await self.dashboard.auth_middleware(adapted)
            if response is not None:
                return jsonify(json.loads(response.body)), response.status_code

        self.client = self.app.test_client()

    async def current(self):
        response = await self.client.get(API, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        return (await response.get_json())["data"]

    async def save(self, percent, revision=None):
        if revision is None:
            revision = (await self.current())["revision"]
        return await self.client.post(API, json={"percent": percent, "revision": revision}, headers=self.headers)

    def disk(self):
        return json.loads(self.path.read_text(encoding="utf-8-sig"))

    async def test_read_current_35_and_external_changes_without_writes(self):
        before = self.path.read_bytes()
        with patch.object(AstrBotConfig, "save_config") as save:
            current = await self.current()
            self.assertEqual(current["percent"], 35)
            self.assertTrue(current["writable"])
            self.assertEqual(current, await self.current())
            self.gallery.config[panel.EMOJI_CONFIG_KEY]["trigger_probability"] = "0.625"
            newer = await self.current()
            self.assertEqual(newer["percent"], 62.5)
            self.assertNotEqual(newer["revision"], current["revision"])
            save.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn("fixture-private-value", json.dumps(current))
        self.assertEqual(self.plugin.config, self.proactive_before)

    async def test_boundaries_decimal_and_live_reader_share_one_persisted_source(self):
        shared = self.gallery.config
        for percent, raw in [(0, "0"), (100, "1"), (35.5, "0.355"), (35, "0.35")]:
            with self.subTest(percent=percent):
                response = await self.save(percent)
                self.assertEqual(response.status_code, 200)
                data = (await response.get_json())["data"]
                self.assertEqual(data["percent"], percent)
                self.assertIs(self.gallery.config, shared)
                expected = copy.deepcopy(self.initial)
                expected[panel.EMOJI_CONFIG_KEY]["trigger_probability"] = raw
                self.assertEqual(dict(shared), expected)
                self.assertEqual(self.disk(), expected)
                self.assertEqual(float(self.gallery._proactive_emoji_config()["trigger_probability"]), percent / 100)
                self.assertEqual(await self.current(), data)
        self.assertEqual(self.plugin.config, self.proactive_before)

    async def test_invalid_percent_types_ranges_and_nonfinite_never_write(self):
        current = await self.current()
        before = self.path.read_bytes()
        with patch.object(AstrBotConfig, "save_config") as save:
            for value in [-1, 100.01, True, False, "35", None, [], {}, float("nan"), float("inf"), -float("inf")]:
                with self.subTest(value=value):
                    response = await self.save(value, current["revision"])
                    self.assertEqual(response.status_code, 400)
            save.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(dict(self.gallery.config), self.initial)

    async def test_malformed_unknown_fields_and_revision_types_rejected(self):
        revision = (await self.current())["revision"]
        for body in [[], {}, {"percent": 20}, {"percent": 20, "revision": None},
                     {"percent": 20, "revision": 1}, {"percent": 20, "revision": ""},
                     {"percent": 20, "revision": revision, "enabled": True}]:
            with self.subTest(body=body):
                response = await self.client.post(API, json=body, headers=self.headers)
                self.assertEqual(response.status_code, 400)
        response = await self.client.post(API, data="{broken", headers={**self.headers, "Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.disk(), self.initial)

    async def test_concurrent_saves_one_wins_and_stale_page_cannot_overwrite(self):
        revision = (await self.current())["revision"]
        responses = await asyncio.gather(self.save(15, revision), self.save(85, revision))
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409])
        self.assertIn((await self.current())["percent"], [15, 85])
        self.assertEqual(self.disk(), dict(self.gallery.config))

    async def test_gallery_lock_and_unrelated_edits_are_respected(self):
        revision = (await self.current())["revision"]
        await self.gallery._lock.acquire()
        pending = asyncio.create_task(self.save(50, revision))
        try:
            await asyncio.sleep(.02)
            self.assertFalse(pending.done())
            self.gallery.config["image_caption"]["provider"] = "new-provider"
            self.gallery.config.save_config()
        finally:
            self.gallery._lock.release()
        response = await asyncio.wait_for(pending, 1)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.disk()["image_caption"]["provider"], "new-provider")
        self.assertEqual(self.disk()[panel.EMOJI_CONFIG_KEY]["trigger_probability"], "0.35")

    async def test_unloaded_and_inactive_gallery_are_read_only(self):
        revision = (await self.current())["revision"]
        for meta in [None, SimpleNamespace(activated=False, star_cls=self.gallery),
                     SimpleNamespace(activated=True, star_cls=None)]:
            self.meta = meta
            current = await self.current()
            self.assertFalse(current["available"])
            self.assertFalse(current["writable"])
            self.assertIn("未加载或未启用", current["message"])
            self.assertEqual((await self.save(50, revision)).status_code, 503)
        self.assertEqual(self.disk(), self.initial)

    async def test_automatic_emoji_disabled_can_preset_without_enabling_or_changing_other_settings(self):
        self.gallery.config[panel.EMOJI_CONFIG_KEY]["enabled"] = False
        self.gallery.config.save_config()
        expected = copy.deepcopy(dict(self.gallery.config))
        current = await self.current()
        self.assertTrue(current["available"])
        self.assertTrue(current["writable"])
        self.assertEqual(current["percent"], 35)
        self.assertIn("当前关闭", current["message"])
        for percent, raw in [(0, "0"), (100, "1"), (62.5, "0.625")]:
            with self.subTest(percent=percent):
                response = await self.save(percent, current["revision"])
                self.assertEqual(response.status_code, 200)
                saved = (await response.get_json())["data"]
                self.assertEqual(saved["percent"], percent)
                self.assertTrue(saved["writable"])
                self.assertIn("当前关闭", saved["message"])
                self.assertIn("不会自动开启", saved["message"])
                expected[panel.EMOJI_CONFIG_KEY]["trigger_probability"] = raw
                self.assertEqual(dict(self.gallery.config), expected)
                self.assertEqual(self.disk(), expected)
                self.assertFalse(self.gallery._proactive_emoji_config()["enabled"])
                current = await self.current()
                self.assertEqual(current, saved)
        self.assertEqual(self.plugin.config, self.proactive_before)

    async def test_instance_change_while_waiting_does_not_write_retired_gallery(self):
        revision = (await self.current())["revision"]
        waiting = asyncio.Event()
        class ObservedLock(asyncio.Lock):
            async def acquire(self):
                if self.locked():
                    waiting.set()
                return await super().acquire()
        self.gallery._lock = ObservedLock()
        await self.gallery._lock.acquire()
        pending = asyncio.create_task(self.save(50, revision))
        try:
            await asyncio.wait_for(waiting.wait(), 5)
            self.assertFalse(pending.done())
            self.meta = None
        finally:
            self.gallery._lock.release()
        self.assertEqual((await asyncio.wait_for(pending, 1)).status_code, 409)
        self.assertEqual(dict(self.gallery.config), self.initial)
        self.assertEqual(self.disk(), self.initial)

    async def test_reloaded_instance_with_same_values_invalidates_revision(self):
        revision = (await self.current())["revision"]
        replacement = object.__new__(SmartImageSenderPlugin)
        replacement.config = self.gallery.config
        replacement._lock = asyncio.Lock()
        self.meta = SimpleNamespace(activated=True, star_cls=replacement)
        self.assertNotEqual((await self.current())["revision"], revision)
        self.assertEqual((await self.save(50, revision)).status_code, 409)
        self.assertEqual(self.disk(), self.initial)

    async def test_atomic_disk_failure_restores_memory_and_reports_unconfirmed_rollback(self):
        original_section = self.gallery.config[panel.EMOJI_CONFIG_KEY]
        with patch("astrbot.core.config.astrbot_config.os.replace", side_effect=OSError("offline disk error")):
            response = await self.save(75)
        self.assertEqual(response.status_code, 500)
        self.assertIn("恢复未能确认", (await response.get_json())["message"])
        self.assertIs(self.gallery.config[panel.EMOJI_CONFIG_KEY], original_section)
        self.assertEqual(dict(self.gallery.config), self.initial)
        self.assertEqual(self.disk(), self.initial)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    async def test_failure_after_commit_compensates_disk_and_memory(self):
        real_save = self.gallery.config.save_config
        calls = 0
        def write_then_fail_once():
            nonlocal calls
            calls += 1
            real_save()
            if calls == 1:
                raise OSError("offline failure after commit")
        with patch.object(AstrBotConfig, "save_config", side_effect=write_then_fail_once):
            response = await self.save(75)
        self.assertEqual(response.status_code, 500)
        self.assertIn("原图库配置已恢复", (await response.get_json())["message"])
        self.assertEqual(calls, 2)
        self.assertEqual(dict(self.gallery.config), self.initial)
        self.assertEqual(self.disk(), self.initial)
        self.assertEqual(self.plugin.config, self.proactive_before)

    async def test_false_persistence_result_is_not_reported_as_success(self):
        with patch.object(AstrBotConfig, "save_config", return_value=False):
            response = await self.save(75)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(dict(self.gallery.config), self.initial)

    async def test_actual_dashboard_auth_rejects_missing_invalid_and_expired_tokens(self):
        for headers in [{}, {"Authorization": "Bearer not-a-token"},
                        {"Authorization": "Bearer " + jwt.encode({"username": "offline-test", "exp": 1},
                           self.dashboard._jwt_secret, algorithm="HS256")}]:
            for method in ["get", "post"]:
                with self.subTest(method=method, headers=bool(headers)):
                    response = await getattr(self.client, method)(API, headers=headers)
                    self.assertEqual(response.status_code, 401)
        self.plugin.context.get_registered_star.assert_not_called()
        self.assertEqual(self.disk(), self.initial)
        self.assertEqual((await self.current())["percent"], 35)


class EmojiProbabilityPage(unittest.TestCase):
    def test_independent_form_does_not_repurpose_unicode_emoji_or_statistics(self):
        html = (PLUGIN / "pages/control/index.html").read_text(encoding="utf-8")
        field = re.search(r'<input[^>]*id="emoji-probability-percent"[^>]*>', html).group()
        self.assertIn('form="emoji-settings-form"', field)
        self.assertIn('min="0"', field)
        self.assertIn('max="100"', field)
        for id_ in ["emoji_probability", "meme_separate_probability", "performance-emoji"]:
            self.assertIn(f'id="{id_}"', html)
        self.assertNotIn("trigger_probability", panel.FIELDS)

    def test_shipped_page_bridge_saves_gallery_only_and_handles_conflicts(self):
        node = shutil.which("node")
        bundled = ROOT / "SnowLuma/node.exe"
        if not node and bundled.is_file():
            node = str(bundled)
        if not node:
            self.skipTest("Node.js is needed for the offline page bridge test")
        html = (PLUGIN / "pages/control/index.html").read_text(encoding="utf-8")
        elements = {id_: tag for tag, id_ in re.findall(r'<(\w+)[^>]*\bid="([^"]+)"', html)}
        script = r"""
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.handlers={};this._text='';this._value='';this.disabled=false;this.checked=false;this.classList={toggle(){}};}
 set textContent(v){this._text=String(v);this.children=[];}
 get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
 set innerHTML(v){throw new Error('Untrusted HTML');}
 set value(v){this._value=String(v);} get value(){return this._value;}
 append(...children){this.children.push(...children);}
 before(...siblings){this.previousSiblings=siblings;}
 after(...siblings){this.nextSiblings=siblings;}
 replaceChildren(){this.children=[];this._text='';}
 addEventListener(name,handler){this.handlers[name]=handler;} setAttribute(){}
}
const nodes=Object.fromEntries(Object.entries(input.elements).map(([id,tag])=>[id,Object.assign(new Element(tag),{id})]));
const posts=[],listeners={};let confirms=true,getCount=0,failRead=false,postError='',release;
let gallery={available:true,writable:true,percent:35,revision:'g1',message:'图库当前35%'};
let settings={...input.values};
const bridge={ready:async()=>{},apiGet:async endpoint=>{
 if(endpoint==='emoji_settings'){getCount++;if(failRead)throw new Error('offline');return {status:'ok',data:{...gallery}};}
 if(endpoint==='panel_settings')return {status:'ok',data:{values:{...settings},revision:'p1'}};
 if(endpoint==='status')return {status:'ok',data:{performance:{emoji:{stats:{sent_separate:4}}}}};
 if(['panel_groups','panel_friends'].includes(endpoint))return {status:'ok',data:[]};
 throw new Error(endpoint);
},apiPost:async(endpoint,body)=>{
 posts.push({endpoint,body});
 if(endpoint==='panel_settings'){settings={...body.values};return {status:'ok',data:{values:{...settings},revision:'p2'}};}
 assert.equal(endpoint,'emoji_settings');assert.deepEqual(Object.keys(body).sort(),['percent','revision']);
 assert.equal(body.revision,gallery.revision);
 if(postError)return {status:'error',message:postError};
 if(release===true)await new Promise(resolve=>release=resolve);
 gallery={...gallery,percent:body.percent,revision:gallery.revision+'n'};
 return {status:'ok',data:{...gallery}};
}};
vm.runInNewContext(input.source,{document:{getElementById:id=>{assert.ok(nodes[id],id);return nodes[id];},createElement:tag=>new Element(tag),querySelectorAll:()=>[]},
 window:{AstrBotPluginView:bridge,addEventListener:(name,f)=>listeners[name]=f,confirm:()=>confirms},setTimeout,clearTimeout});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const submit=()=>nodes['emoji-settings-form'].handlers.submit({preventDefault(){}});
const reload=()=>nodes['reload-emoji-probability'].handlers.click();
const edit=v=>{nodes['emoji-probability-percent'].value=v;nodes['emoji-probability-percent'].handlers.input();nodes.settings.handlers.input({target:nodes['emoji-probability-percent']});};
(async()=>{
 await tick();await tick();
 assert.equal(nodes['emoji-probability-percent'].value,'35');assert.equal(nodes['emoji-probability-percent'].disabled,false);
 assert.equal(nodes.emoji_probability.value,'18');
 edit('65.5');assert.equal(nodes['save-state'].textContent,'已与当前配置同步');
 let warned=false;listeners.beforeunload({preventDefault(){warned=true;}});assert.equal(warned,true);
 await submit();assert.equal(posts.length,1);assert.equal(posts[0].body.percent,65.5);
 assert.equal(posts[0].endpoint,'emoji_settings');assert.equal(nodes.emoji_probability.value,'18');
 assert.equal(nodes['save-emoji-probability'].disabled,true);
 for(const value of ['', '-1', '101', 'NaN', 'Infinity']){edit(value);await submit();assert.equal(posts.length,1);}
 edit('70');postError='版本冲突，请重新读取';await submit();
 assert.equal(nodes['emoji-probability-percent'].value,'70');assert.equal(nodes['save-emoji-probability'].disabled,true);
 assert.match(nodes['emoji-probability-status'].textContent,/冲突/);
 const oldGets=getCount;confirms=false;await reload();assert.equal(getCount,oldGets);
 confirms=true;postError='';gallery.percent=80;gallery.revision='external';await reload();assert.equal(nodes['emoji-probability-percent'].value,'80');
 edit('85');release=true;const pending=submit();await tick();
 assert.equal(nodes['emoji-probability-percent'].disabled,true);const count=posts.length;
 await submit();await reload();assert.equal(posts.length,count);release();await pending;release=null;
 assert.equal(nodes['emoji-probability-percent'].value,'85');
 edit('25');await nodes.settings.handlers.submit({preventDefault(){}});
 assert.equal(posts.at(-1).endpoint,'panel_settings');assert.equal(nodes['emoji-probability-percent'].value,'25');
 assert.equal(Object.hasOwn(posts.at(-1).body.values,'trigger_probability'),false);
 assert.equal(posts.at(-1).body.values.emoji_probability,18);
 assert.equal(nodes['save-emoji-probability'].disabled,false);
 await nodes['refresh-performance'].handlers.click();assert.match(nodes['performance-emoji'].textContent,/已单独发送4/);
 assert.equal(nodes['emoji-probability-percent'].value,'25');
 gallery={available:false,writable:false,percent:null,revision:null,message:'图库未加载'};await reload();
 assert.equal(nodes['emoji-probability-percent'].disabled,true);assert.match(nodes['emoji-probability-status'].textContent,/未加载/);
 const noWrite=posts.length;await submit();assert.equal(posts.length,noWrite);
 gallery={available:true,writable:true,percent:35,revision:'off',message:'图库自动配图当前关闭，可以预先设置概率；保存概率不会自动开启'};await reload();
 assert.equal(nodes['emoji-probability-percent'].value,'35');assert.equal(nodes['emoji-probability-percent'].disabled,false);
 assert.match(nodes['emoji-probability-status'].textContent,/当前关闭/);
 edit('62.5');assert.equal(nodes['save-emoji-probability'].disabled,false);await submit();
 assert.equal(posts.at(-1).endpoint,'emoji_settings');assert.equal(posts.at(-1).body.percent,62.5);
 assert.equal(nodes['emoji-probability-percent'].value,'62.5');
 assert.match(nodes['emoji-probability-status'].textContent,/已保存/);
 assert.match(nodes['emoji-probability-status'].textContent,/当前关闭/);
 assert.match(nodes['emoji-probability-status'].textContent,/不会自动开启/);
 failRead=true;await reload();assert.match(nodes['emoji-probability-status'].textContent,/读取失败/);
 assert.equal(nodes['reload-emoji-probability'].disabled,false);
 console.log('emoji probability page bridge passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([node, "-e", script], input=json.dumps({"elements": elements,
            "values": panel.FIELDS, "source": (PLUGIN / "pages/control/app.js").read_text(encoding="utf-8")}),
            text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
