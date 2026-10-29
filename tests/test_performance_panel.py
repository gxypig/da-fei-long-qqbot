"""Panel contract tests, independent of Main and the live bot's data directory."""
import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from quart import Quart


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "AstrBot/data/plugins/astrbot_plugin_proactive_agent"
SPEC = importlib.util.spec_from_file_location("performance_panel_under_test", PLUGIN / "panel.py")
panel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(panel)

DEFAULTS = {
    "latency_optimization_enabled": False,
    "review_policy": "all",
    "reply_prediction_enabled": True,
    "prediction_provider_id": "",
    "idle_learning_enabled": False,
    "style_learning_enabled": False,
    "reply_audit_sample_percent": 10,
    "emoji_late_grace_seconds": 3,
    "reply_latency_target_seconds": 30,
    "model_concurrency": 2,
    "reply_hedge_after_seconds": 15,
    "context_window_enabled": False,
    "context_recent_turns": 20,
    "context_recent_tokens": 10000,
}
RANGES = {
    "reply_audit_sample_percent": (0, 100),
    "emoji_late_grace_seconds": (0, 10),
    "reply_latency_target_seconds": (5, 120),
    "model_concurrency": (1, 4),
    "reply_hedge_after_seconds": (0, 30),
    "context_recent_turns": (4, 40),
    "context_recent_tokens": (2000, 16000),
}
PREFIX = "/astrbot_plugin_proactive_agent/"


class Config(dict):
    def save_config(self):
        self.saved = copy.deepcopy(dict(self))


class PanelPlugin(panel.PanelMixin):
    def __init__(self, config):
        self.config = Config(config)
        self.context = SimpleNamespace(register_web_api=Mock())

    def _cfg(self, key, default=None):
        value = self.config.get(key, default)
        return default if value is None else value


class PerformanceValidationTests(unittest.TestCase):
    def test_schema_and_panel_share_requested_defaults(self):
        schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))
        types = {bool: "bool", int: "int", str: "string"}
        for key, value in DEFAULTS.items():
            with self.subTest(key=key):
                self.assertEqual(panel.FIELDS[key], value)
                self.assertIs(type(panel.FIELDS[key]), type(value))
                self.assertEqual(schema[key]["default"], value)
                self.assertEqual(schema[key]["type"], types[type(value)])
        self.assertEqual(schema["review_policy"]["options"], ["all", "speed_first"])
        self.assertEqual(panel.validate(dict(panel.FIELDS)), panel.FIELDS)

    def test_legacy_config_gets_defaults_without_mutation_or_revision_drift(self):
        legacy = {k: v for k, v in panel.FIELDS.items() if k not in DEFAULTS}
        plugin = PanelPlugin({**legacy, "custom_prompt": "retained"})
        before = dict(plugin.config)
        values, revision = panel.settings(plugin)
        self.assertEqual({k: values[k] for k in DEFAULTS}, DEFAULTS)
        self.assertEqual(dict(plugin.config), before)
        explicit = PanelPlugin({**legacy, **DEFAULTS})
        self.assertEqual(panel.settings(explicit)[1], revision)
        for key in DEFAULTS:
            plugin.config[key] = None
        self.assertEqual(panel.settings(plugin), (values, revision))

    def test_numeric_boundaries_and_strict_integer_types(self):
        for key, (low, high) in RANGES.items():
            for value in (low, high):
                with self.subTest(key=key, value=value):
                    self.assertEqual(panel.validate({**panel.FIELDS, key: value})[key], value)
            for value in (low - 1, high + 1, True, False, float(low), str(low), None):
                with self.subTest(key=key, value=value):
                    with self.assertRaises(ValueError):
                        panel.validate({**panel.FIELDS, key: value})

    def test_policy_booleans_and_provider_types(self):
        for policy in ("all", "speed_first"):
            self.assertEqual(panel.validate({**panel.FIELDS, "review_policy": policy})["review_policy"], policy)
        for value in ("none", "ALL", "", None, 1):
            with self.subTest(policy=value), self.assertRaises(ValueError):
                panel.validate({**panel.FIELDS, "review_policy": value})
        for key, default in DEFAULTS.items():
            if type(default) is bool:
                for value in ("true", 1, 0, None):
                    with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                        panel.validate({**panel.FIELDS, key: value})
        self.assertEqual(panel.validate({**panel.FIELDS, "prediction_provider_id": "  provider/test  "})["prediction_provider_id"], "provider/test")
        for value in (None, 123, False, []):
            with self.subTest(provider=value), self.assertRaises(ValueError):
                panel.validate({**panel.FIELDS, "prediction_provider_id": value})

    def test_stale_clients_and_unknown_fields_require_refresh(self):
        for key in DEFAULTS:
            values = {k: v for k, v in panel.FIELDS.items() if k != key}
            with self.subTest(missing=key), self.assertRaisesRegex(ValueError, "刷新"):
                panel.validate(values)
        with self.assertRaisesRegex(ValueError, "刷新"):
            panel.validate({**panel.FIELDS, "unknown": True})


class PerformancePanelAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = PanelPlugin({"custom_prompt": "keep", "unexposed": {"retain": True}})
        self.plugin.register_panel()
        self.app = Quart(__name__)
        # Mount only the routes registered through the existing native mechanism.
        for call in self.plugin.context.register_web_api.call_args_list:
            route, handler, methods, _ = call.args
            self.app.add_url_rule(route, view_func=handler, methods=methods)
        self.client = self.app.test_client()

    async def current(self):
        response = await self.client.get(PREFIX + "panel_settings")
        self.assertEqual(response.status_code, 200)
        return (await response.get_json())["data"]

    def test_routes_preserve_native_registration_and_explicit_control_endpoints(self):
        registered = {call.args[0]: call.args[2] for call in self.plugin.context.register_web_api.call_args_list}
        self.assertEqual(registered, {
            PREFIX + "panel_settings": ["GET", "POST"],
            PREFIX + "emoji_settings": ["GET", "POST"],
            PREFIX + "panel_groups": ["GET"],
            PREFIX + "panel_friends": ["GET"],
            PREFIX + "panel_schedule": ["GET"],
            PREFIX + "panel_health": ["GET"],
            PREFIX + "learning_action": ["POST"],
            PREFIX + "learning_content": ["GET"],
            PREFIX + "meme_repository": ["GET", "POST"],
            PREFIX + "meme_wiki": ["GET", "POST"],
            PREFIX + "panel_review_metrics": ["GET"],
            PREFIX + "panel_capabilities": ["GET"],
        })
        self.assertEqual(len(registered), self.plugin.context.register_web_api.call_count)

    async def test_save_updates_shared_live_config_preserves_other_keys(self):
        shared = self.plugin.config
        reader = self.plugin._cfg
        body = await self.current()
        self.assertFalse(hasattr(shared, "saved"))
        self.assertEqual({k: body["values"][k] for k in DEFAULTS}, DEFAULTS)
        body["values"].update(
            latency_optimization_enabled=True, review_policy="speed_first",
            idle_learning_enabled=True, style_learning_enabled=True,
            reply_prediction_enabled=False, prediction_provider_id="predictor",
            model_concurrency=4, reply_audit_sample_percent=100,
            emoji_late_grace_seconds=0, reply_latency_target_seconds=5,
        )
        response = await self.client.post(PREFIX + "panel_settings", json=body)
        self.assertEqual(response.status_code, 200)
        saved = (await response.get_json())["data"]
        self.assertIs(self.plugin.config, shared)
        for key in DEFAULTS:
            self.assertEqual(reader(key), body["values"][key])
            self.assertEqual(shared.saved[key], body["values"][key])
        self.assertEqual(shared["custom_prompt"], "keep")
        self.assertEqual(shared["unexposed"], {"retain": True})
        self.assertEqual(saved["values"], body["values"])
        self.assertNotEqual(saved["revision"], body["revision"])

    async def test_hedge_wait_round_trips_zero_and_limit_preserving_other_settings(self):
        original = await self.current()
        key = "reply_hedge_after_seconds"
        self.assertEqual(original["values"][key], 15)
        unchanged = {k: v for k, v in original["values"].items() if k != key}
        for seconds in (0, 30, 15):
            with self.subTest(seconds=seconds):
                body = await self.current()
                body["values"][key] = seconds
                response = await self.client.post(PREFIX + "panel_settings", json=body)
                self.assertEqual(response.status_code, 200)
                saved = (await response.get_json())["data"]
                self.assertEqual(saved["values"][key], seconds)
                self.assertIs(type(saved["values"][key]), int)
                self.assertEqual(self.plugin._cfg(key, 15), seconds)
                self.assertEqual(self.plugin.config.saved[key], seconds)
                self.assertEqual({k: v for k, v in saved["values"].items() if k != key}, unchanged)
                self.assertNotEqual(saved["revision"], body["revision"])
                self.assertEqual(await self.current(), saved)
                conflict = await self.client.post(PREFIX + "panel_settings", json=body)
                self.assertEqual(conflict.status_code, 409)
        before = copy.deepcopy(dict(self.plugin.config))
        for invalid in (-1, 31, True, False, 15.5, "15", None):
            with self.subTest(invalid=invalid):
                body = await self.current()
                body["values"][key] = invalid
                response = await self.client.post(PREFIX + "panel_settings", json=body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(dict(self.plugin.config), before)

    async def test_invalid_or_missing_fields_do_not_partially_save(self):
        before = dict(self.plugin.config)
        for key in DEFAULTS:
            with self.subTest(key=key):
                body = await self.current()
                del body["values"][key]
                response = await self.client.post(PREFIX + "panel_settings", json=body)
                self.assertEqual(response.status_code, 400)
                self.assertIn("刷新", (await response.get_json())["message"])
        for key, value in [("model_concurrency", True), ("reply_audit_sample_percent", 101),
                           ("review_policy", "skip"), ("quiet_hours", "25:00-01:00")]:
            body = await self.current()
            body["values"][key] = value
            response = await self.client.post(PREFIX + "panel_settings", json=body)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(dict(self.plugin.config), before)
        self.assertFalse(hasattr(self.plugin.config, "saved"))

    async def test_new_fields_participate_in_optimistic_lock(self):
        body = await self.current()
        self.plugin.config["model_concurrency"] = 3
        response = await self.client.post(PREFIX + "panel_settings", json=body)
        self.assertEqual(response.status_code, 409)
        self.assertIn("刷新", (await response.get_json())["message"])
        self.assertEqual(self.plugin.config["model_concurrency"], 3)
        self.assertFalse(hasattr(self.plugin.config, "saved"))

    async def test_concurrent_saves_do_not_overwrite_each_other(self):
        first = await self.current()
        second = copy.deepcopy(first)
        first["values"]["model_concurrency"] = 1
        second["values"]["model_concurrency"] = 4
        responses = await asyncio.gather(*[
            self.client.post(PREFIX + "panel_settings", json=body) for body in (first, second)
        ])
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409])
        winner = first if responses[0].status_code == 200 else second
        self.assertEqual(self.plugin.config["model_concurrency"], winner["values"]["model_concurrency"])

    async def test_save_failure_restores_live_config_and_revision(self):
        before = dict(self.plugin.config)
        shared = self.plugin.config
        body = await self.current()
        revision = body["revision"]
        body["values"]["latency_optimization_enabled"] = True
        with patch.object(shared, "save_config", side_effect=OSError("disk full")):
            response = await self.client.post(PREFIX + "panel_settings", json=body)
        self.assertEqual(response.status_code, 500)
        self.assertIs(shared, self.plugin.config)
        self.assertEqual(dict(shared), before)
        self.assertEqual((await self.current())["revision"], revision)

    async def test_rollback_awaits_service_with_exact_version_and_returns_snapshot(self):
        before = dict(self.plugin.config)
        for version in (0, 3, "v3"):
            snapshot = {"active_version": version, "pending": 0}
            rollback = AsyncMock(return_value=snapshot)
            self.plugin._learning = SimpleNamespace(rollback=rollback)
            response = await self.client.post(PREFIX + "learning_action", json={"action": "rollback", "version": version})
            self.assertEqual(response.status_code, 200)
            self.assertEqual((await response.get_json())["data"], snapshot)
            rollback.assert_awaited_once_with(version)
        self.assertEqual(dict(self.plugin.config), before)

    async def test_missing_or_unready_learning_service(self):
        for service in ("absent", None, SimpleNamespace()):
            if service != "absent":
                self.plugin._learning = service
            response = await self.client.post(PREFIX + "learning_action", json={"action": "rollback", "version": 0})
            self.assertEqual(response.status_code, 503)
            self.assertIn("学习服务", (await response.get_json())["message"])

    async def test_only_rollback_with_valid_body_can_reach_service(self):
        rollback = AsyncMock()
        self.plugin._learning = SimpleNamespace(rollback=rollback)
        invalid = [[], {}, {"action": "rollback"}, {"action": "delete", "version": 0},
                   {"action": "rollback", "version": 1, "extra": True}]
        invalid += [{"action": "rollback", "version": v} for v in (None, True, False, -1, 1.5, "", "  ", [], {})]
        for body in invalid:
            with self.subTest(body=body):
                response = await self.client.post(PREFIX + "learning_action", json=body)
                self.assertEqual(response.status_code, 400)
        response = await self.client.post(PREFIX + "learning_action", data="{broken", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual((await self.client.get(PREFIX + "learning_action")).status_code, 405)
        rollback.assert_not_awaited()

    async def test_rollback_failure_is_reported_without_internal_details(self):
        for failure, status in [(ValueError("private detail"), 400), (KeyError("private detail"), 400), (OSError("private detail"), 500)]:
            with self.subTest(failure=type(failure).__name__):
                self.plugin._learning = SimpleNamespace(rollback=AsyncMock(side_effect=failure))
                response = await self.client.post(PREFIX + "learning_action", json={"action": "rollback", "version": 9})
                self.assertEqual(response.status_code, status)
                result = await response.get_json()
                self.assertEqual(result["status"], "error")
                self.assertNotIn("private detail", result["message"])
        self.plugin._learning = SimpleNamespace(rollback=AsyncMock(return_value=False))
        response = await self.client.post(PREFIX + "learning_action", json={"action": "rollback", "version": 9})
        self.assertEqual(response.status_code, 404)


class NativePageTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for the native page bridge test")
    def test_page_bridge_status_rollback_and_unsaved_settings(self):
        # Execute the shipped script with a DOM/bridge double; never contact the bot.
        html = (PLUGIN / "pages/control/index.html").read_text(encoding="utf-8")
        elements = {id_: tag for tag, id_ in re.findall(r'<(\w+)[^>]*\bid="([^"]+)"', html)}
        self.assertTrue(set(panel.FIELDS).issubset(elements))
        script = r"""
const assert=require('node:assert/strict'), vm=require('node:vm'), fs=require('node:fs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.handlers={};this._text='';this._value='';this.disabled=false;this.checked=false;this.classList={toggle(){}};}
  set textContent(v){this._text=String(v);this.children=[];}
  get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
  set innerHTML(v){throw new Error('Status data must never be interpreted as HTML');}
  set value(v){this._value=String(v);}
  get value(){return this._value;}
  append(...children){this.children.push(...children);if(this.tag==='select'&&!this._value)this._value=this.children[0]?.value||'';}
  before(...siblings){this.previousSiblings=siblings;}
  after(...siblings){this.nextSiblings=siblings;}
  replaceChildren(){this.children=[];this._text='';if(this.tag==='select')this._value='';}
  addEventListener(name,handler){this.handlers[name]=handler;}
  setAttribute(){}
}
const nodes=Object.fromEntries(Object.entries(input.elements).map(([id,tag])=>[id,Object.assign(new Element(tag),{id})]));
const posts=[];
let settings={...input.values}, revision='r1', payload={}, failure=false, releaseRollback;
const bridge={
  ready:async()=>{},
  apiGet:async endpoint=>{
    if(endpoint==='panel_settings')return {status:'ok',data:{values:{...settings},revision}};
    if(endpoint==='status'){if(failure)throw new Error('offline');return {status:'ok',data:payload};}
    if(['panel_groups','panel_friends'].includes(endpoint))return {status:'ok',data:[]};
    throw new Error('Unexpected GET: '+endpoint);
  },
  apiPost:async (endpoint,body)=>{
    posts.push({endpoint,body});
    if(endpoint==='panel_settings'){
      assert.equal(body.revision,revision);settings={...body.values};revision='r2';
      return {status:'ok',data:{values:{...settings},revision}};
    }
    assert.equal(endpoint,'learning_action');
    await new Promise(resolve=>{releaseRollback=resolve;});
    return {status:'ok',data:{active_version:body.version}};
  }
};
vm.runInNewContext(input.source,{
  document:{getElementById:id=>{assert.ok(nodes[id],'Missing HTML element: '+id);return nodes[id];},createElement:tag=>new Element(tag),querySelectorAll:()=>[]},
  window:{AstrBotPluginView:bridge,addEventListener(){},confirm:()=>true},setTimeout,clearTimeout,
});
const click=id=>nodes[id].handlers.click();
const refresh=()=>click('refresh-performance');
const current=()=>({performance:{enabled:false,review_policy:'all',target_seconds:30,
  work:{scheduler:{queued:0},extra:{detail:'<img src=x onerror=alert(1)>'}},prediction:{hits:0},
  emoji:{trigger_probability:'0.35',stats:{triggered:7,sent_combined:2,sent_separate:4,expired:1}},
  timings:{chat:{p95_ms:12000,max_ms:14000,median_ms:10000,over_target:0},tools:{count:0}},
  learning:{active_version:2,pending:0,running:0,worker_running:true,cases:4,last_validation:{passed:true},
    versions:[{version:2,published:123,rules:3},{version:1},null,{},true]}}});
(async()=>{
  await new Promise(setImmediate);
  assert.equal(nodes.save.disabled,false);
  assert.equal(nodes.reply_prediction_enabled.checked,true);
  assert.equal(nodes.reply_hedge_after_seconds.value,'15');
  nodes.reply_hedge_after_seconds.value='0';
  nodes.model_concurrency.value='4';nodes.settings.handlers.input({target:nodes.model_concurrency});
  payload=current();await refresh();
  assert.equal(nodes.model_concurrency.value,'4');
  assert.match(nodes['save-state'].textContent,/未保存/);
  assert.match(nodes['performance-status'].textContent,/30/);
  assert.ok(nodes['performance-work'].textContent.includes('<img src=x onerror=alert(1)>'));
  assert.match(nodes['performance-work'].textContent,/模型调度/);
  assert.match(nodes['performance-emoji'].textContent,/尝试配图概率（0至1）0.35/);
  assert.match(nodes['performance-emoji'].textContent,/已单独发送4/);
  assert.match(nodes['performance-timings'].textContent,/P95耗时（毫秒）12000/);
  assert.match(nodes['performance-timings'].textContent,/超出目标次数0/);
  assert.match(nodes['learning-details'].textContent,/运行中作业数0/);
  assert.match(nodes['learning-details'].textContent,/学习后台存活是/);
  assert.match(nodes['learning-details'].textContent,/最近验证/);
  assert.match(nodes['learning-status'].textContent,/学习后台运行中/);
  payload.performance.learning.worker_running=false;await refresh();
  assert.match(nodes['learning-status'].textContent,/学习后台未运行/);
  assert.equal(nodes['learning-rollback'].disabled,false);
  assert.equal(nodes['learning-pending'].textContent,'0');
  assert.match(nodes['learning-version'].textContent,/2/);
  assert.equal(nodes['learning-rollback'].disabled,false);
  assert.ok(nodes['learning-rollback-version'].children.some(o=>o.value==='0'));
  for(const data of [null,{}, {performance:null},{performance:[]},{performance:{learning:null,work:false,timings:[],prediction:'offline'}}, {performance:{learning:['unavailable']}}]){
    payload=data;await refresh();assert.equal(nodes['learning-rollback'].disabled,true);
  }
  payload=current();await refresh();nodes['learning-rollback-version'].value='0';
  const pending=click('learning-rollback');await click('learning-rollback');
  assert.equal(posts.length,1);assert.equal(posts[0].body.action,'rollback');assert.equal(posts[0].body.version,0);
  payload={performance:{...payload.performance,learning:{active_version:0,closed:true,versions:[]}}};
  releaseRollback();await pending;
  assert.match(nodes['learning-action-status'].textContent,/已回退/);
  assert.equal(nodes['learning-rollback'].disabled,true);
  assert.equal(nodes.model_concurrency.value,'4');
  payload=current();await refresh();failure=true;await refresh();
  assert.match(nodes['performance-status'].textContent,/失败/);
  assert.equal(nodes['learning-rollback'].disabled,true);
  failure=false;payload=current();await refresh();
  nodes.review_policy.value='speed_first';nodes.prediction_provider_id.value='predictor';
  await nodes.settings.handlers.submit({preventDefault(){}});
  assert.equal(posts[1].endpoint,'panel_settings');
  assert.equal(posts[1].body.values.model_concurrency,4);
  assert.equal(posts[1].body.values.reply_hedge_after_seconds,0);
  assert.equal(nodes.reply_hedge_after_seconds.value,'0');
  assert.equal(posts[1].body.values.review_policy,'speed_first');
  assert.equal(posts[1].body.values.prediction_provider_id,'predictor');
  assert.deepEqual(Object.keys(posts[1].body.values).sort(),Object.keys(input.values).sort());
  assert.equal(nodes['save-state'].textContent,'已与当前配置同步');
  delete settings.reply_hedge_after_seconds;await click('reload');await new Promise(setImmediate);
  assert.equal(nodes.save.disabled,true);assert.match(nodes.notice.textContent,/刷新/);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            input=json.dumps({"elements": elements, "values": panel.FIELDS,
                              "source": (PLUGIN / "pages/control/app.js").read_text(encoding="utf-8")}),
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
