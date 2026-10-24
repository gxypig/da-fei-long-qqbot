"""Opt-in live-model latency probe, synthetic chat + fake QQ transport only.

Runs the real prompt enrichment, shared risk/emoji prediction, final review,
provider admission, and send guard. No tool is executed and no QQ is sent.
The image candidate index is real; image conversion/transport are local doubles.
Reports complete reviewed text delivery, never a placeholder/first token.
"""
import argparse
import asyncio
import copy
import hashlib
import json
import logging
from pathlib import Path
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tests'), str(ROOT)]
# This import moves AstrBot's initialization into an isolated temporary folder.
from test_maintenance import Main
from test_async_emoji import Event
from aiocqhttp import CQHttp
from astrbot.api import logger
from astrbot.api.event import MessageEventResult
from astrbot.api.message_components import Plain
from astrbot.core.agent.message import Message, TextPart
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from data.plugins.astrbot_plugin_proactive_agent.auxiliary_lane import AuxiliaryLane
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import ReplyReviewer, bind_request, install_outbound_guard
from data.plugins.astrbot_plugin_proactive_agent.conversation_style import MemeHints
from data.plugins.astrbot_plugin_proactive_agent.idle_learning import IdleLearning
from data.plugins.astrbot_plugin_proactive_agent.meme_memory import MemeMemory
from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import WorkRuntime, install_work_runtime
from data.plugins.astrbot_plugin_smart_imagechat_hub.backend.retrieval import RetrievalMixin
from data.plugins.astrbot_plugin_smart_imagechat_hub.main import SmartImageSenderPlugin
from tools.local_admin import api

PROMPTS = [
    '下班了，今天决定奖励自己一杯奶茶', '昨晚熬夜，现在困得眼睛都睁不开',
    '刚才打游戏赢了，今天状态不错', '我今天竟然赶上了最后一班车',
    '中午不知道吃什么，纠结死了', '本来打算早睡，结果又刷了两个小时手机',
    '周末躺了一整天，感觉自己像个土豆', '天气又突然冷了，我的短袖白穿了',
    '刚倒了杯水就被我碰翻了，服了', '朋友吹了一晚上牛，最后游戏一把都没赢',
    '我买的猫窝猫不睡，偏偏喜欢快递纸箱', '上班像充电五分钟，掉电两小时',
    '今晚终于把桌子收拾干净了，舒服', '看到一只小狗跑得耳朵都飞起来了',
    '你今天看起来还挺精神', '今天阳光正好，想出去走两圈',
    '奶茶刚拿到就撒了一半，我心态崩了', '闹钟响了五次，我愣是一次都没醒',
    '哈哈哈，刚才那个乌龙笑得我肚子疼', '今天难过，想找个角落待一会儿',
    '典中典，队友又开始甩锅了', '这下真绷不住了，怎么又是我背锅',
    '啊对对对，这局全靠他在旁边看', '我家的猫又在键盘上睡觉',
    '今天的晚霞是粉紫色的，看得人心情都好了', '等外卖等到不饿了也是一种本事',
    '地铁刚到我就到了，简直踩点大师', '这杯咖啡苦得我感觉人生都清醒了',
    '你这嘴也太欠了，快认错', '别闹了，我现在真的有点烦',
    '帮我写一段Python代码', '你能给我讲一串复杂的积分公式吗',
    '别像客服一样和我说话，随便聊点什么', '我又忘了给手机充电，只剩百分之一',
    '洗完头才发现吹风机坏了，救命', '今天出门忘了带伞，又刚好下雨',
    '新买的鞋走路吱吱响，尴尬得想原地消失', '晚上想吃炸鸡，又怕吃撑',
    '朋友带我飞，结果两个人一起掉坑里了', '刚刚发呆发了十分钟，舒服得很',
    '看个电影把我哭得一把鼻涕一把泪', '学会一道新菜，感觉自己有点厉害',
    '今天我的运气简直逆天，抽到喜欢的东西了', '我认真计划了半天，最后决定继续躺着',
    '家里零食又没了，才买了两天啊', '大肥龙，你有时候还挺可爱的',
    '刚刚差点把洗面奶当牙膏，我真是绝了', '外面风太大，发型直接自由发挥了',
    '同事夸我今天气色好，其实只是灯比较黄', '今天没什么大事，就是想来聊两句',
]


def summary(rows):
    ok = [r['elapsed_ms'] for r in rows if r.get('ok')]
    ordered = sorted(ok)
    return {'attempts': len(rows), 'complete_texts': len(ok),
            'failures': sum(not r.get('ok') for r in rows),
            'median_ms': round(statistics.median(ok)) if ok else None,
            'p95_ms': ordered[max(0, (len(ordered)*95+99)//100-1)] if ok else None,
            'max_ms': max(ok) if ok else None,
            'within_30s': sum(v <= 30000 for v in ok),
            'over_30s': sum(v > 30000 for v in ok)}


async def run(args):
    logger.setLevel(logging.WARNING)
    cfg = json.loads((ROOT/'AstrBot/data/cmd_config.json').read_text(encoding='utf-8-sig'))
    provider_id = args.provider or cfg.get('provider_settings', {}).get('default_provider_id')
    model = next((p for p in cfg['provider'] if p.get('id') == provider_id), None)
    if model is None:
        model = next(p for p in cfg['provider'] if p.get('enable', True) and p.get('type') in {'chat_completion', 'openai_chat_completion', None})
    source = next(p for p in cfg['provider_sources'] if p['id'] == model['provider_source_id'])
    provider = ProviderOpenAIOfficial({**source, **model}, cfg['provider_settings'])
    advertised = api('/api/tools/list')['data']
    active = [r for r in advertised if r.get('active') and
              (not r.get('builtin_config_statuses') or any(s.get('enabled') and s.get('conf_id') == 'default' for s in r['builtin_config_statuses']))]
    tools = ToolSet([FunctionTool(name=r['name'], description=r['description'], parameters=r['parameters']) for r in active])
    persona = ''
    with sqlite3.connect(f"file:{(ROOT/'AstrBot/data/data_v4.db').as_posix()}?mode=ro", uri=True) as db:
        row = db.execute('SELECT system_prompt FROM personas WHERE persona_id=?', (args.persona,)).fetchone()
        if row: persona = row[0]
    report = {'kind': 'live_model_synthetic_chat_fake_QQ', 'qq_sends': 0,
              'model': model['model'], 'advertised_tools': [r['name'] for r in active],
              'persona_loaded': bool(persona),
              'note': 'Real model, prompt enrichment/review/provider scheduler, local meme lookup and full-index emoji recall. Tools never executed; image conversion and QQ delivery mocked. Sequential requests, not a production p95 guarantee.',
              'requested_count': args.count, 'start_case': args.start_case, 'samples': []}
    report['started_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    report['source_sha256'] = {name: hashlib.sha256((ROOT/'AstrBot/data/plugins/astrbot_plugin_proactive_agent'/name).read_bytes()).hexdigest()
                               for name in ('main.py', 'reply_pipeline.py', 'work_scheduler.py', 'conversation_review.py', 'idle_learning.py')}
    report['hedge_after_seconds'] = args.hedge_after
    outfile = ROOT/args.report
    outfile.parent.mkdir(parents=True, exist_ok=True)
    def save():
        report['summary'] = summary(report['samples'])
        temporary = outfile.with_suffix('.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(outfile)
    with tempfile.TemporaryDirectory(prefix='qqbot-pipeline-probe-') as directory:
        data = Path(directory)
        plugin = object.__new__(Main)
        plugin.config = json.loads((ROOT/'AstrBot/data/config/astrbot_plugin_proactive_agent_config.json').read_text(encoding='utf-8-sig'))
        plugin.config.update(latency_optimization_enabled=True, review_policy='speed_first', reply_prediction_enabled=True,
                             prediction_provider_id='', idle_learning_enabled=True, style_learning_enabled=True,
                             reply_audit_sample_percent=10, model_concurrency=2, emoji_late_grace_seconds=3,
                             reply_hedge_after_seconds=args.hedge_after)
        plugin._data_dir, plugin._timings, plugin._recent_messages = data, {}, {}
        plugin._init_reply_pipeline()
        plugin._auxiliary_lane = AuxiliaryLane()
        gallery = ROOT/'AstrBot/data/plugin_data/astrbot_plugin_smart_imagechat_hub'
        plugin._meme_hints = MemeHints(gallery/'image_index.json')
        source_memory = ROOT/'AstrBot/data/plugin_data/astrbot_plugin_proactive_agent/meme_memory.json'
        if source_memory.exists():
            shutil.copyfile(source_memory, data/'memory.json')
        else:
            (data/'memory.json').write_text('{"entries":{}}',encoding='utf-8')
        plugin._memory = MemeMemory(data/'memory.json', plugin._generate_json, AsyncMock(side_effect=AssertionError('no benchmark web search')), plugin._audit)
        plugin._work = WorkRuntime(plugin._cfg, plugin._audit)
        plugin._reviewer = ReplyReviewer(plugin._generate_json, plugin._audit)
        plugin._learning = IdleLearning(data/'learning.sqlite3', plugin._cfg, plugin._memory, plugin._generate_json,
                                        plugin._reviewer, plugin._audit, plugin._work.busy)
        bot = object.__new__(CQHttp)
        plugin._platform = lambda: SimpleNamespace(bot=bot)
        plugin.context = SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider), get_provider_by_id=lambda _:provider,
                                         get_config=lambda **kwargs:cfg)
        # The idle queue is real and persistent, but no background worker is started
        # in this latency probe. Separate regression tests exercise its lifecycle.
        index_file = gallery/'image_index.json'
        index = json.loads(index_file.read_text(encoding='utf-8')) if index_file.exists() else {'images':{}}
        rows = []
        for key, original in index['images'].items():
            item = copy.deepcopy(original)
            item.setdefault('id', key)
            item.setdefault('rel_path', key)
            item.setdefault('filename', Path(item['rel_path']).name)
            item['tags'] = list(dict.fromkeys(sum([item.get(k, []) for k in ('tags', 'auto_tags', 'manual_tags', 'selected_global_tags') if isinstance(item.get(k, []), list)], [])))
            if item['tags']: rows.append(item)
        report['gallery_candidates'] = len(rows)
        image_plugin = RetrievalMixin()
        image_plugin._init_latency_emoji()
        image_plugin.context = SimpleNamespace(get_registered_star=lambda _:SimpleNamespace(activated=True,star_cls=plugin), send_message=AsyncMock(return_value=True))
        image_plugin._proactive_emoji_config = lambda: {'enabled':True, 'retrieval_mode':'bot_reply_serial', 'meme_only':True, 'embed_in_conversation':True, 'trigger_probability':'1', 'analysis_provider_id':''}
        image_plugin._proactive_emoji_candidates = lambda _: copy.deepcopy(rows)
        image_plugin._proactive_emoji_probability_hit = lambda _: True
        image_plugin._abs_plugin_data_path = lambda path:gallery/path
        image_plugin._tags_from_item = lambda row:row['tags']
        image_plugin._prepare_send_image_path = AsyncMock(side_effect=lambda path,tags:(path,[]))
        image_plugin._cleanup_temp_paths = lambda paths:None
        image_plugin._after_plugin_sent_image_for_meme_combat = AsyncMock()
        image_plugin._send_pending_proactive_emoji = AsyncMock()
        image_plugin._send_pending_meme_combat_burst = AsyncMock()
        image_plugin._cleanup_deferred_send_image_style_paths = Mock()
        image_plugin._log_proactive_emoji_debug = Mock()
        image_plugin._log_proactive_emoji_error_debug = Mock()
        async def fake_send(bot, action, **params):
            if action not in {'send_group_msg','send_private_msg'}:
                raise AssertionError('Only synthetic text sends are permitted')
            return {'message_id':'synthetic-receipt'}
        restore_work = restore_guard = lambda:None
        try:
            with patch.object(CQHttp, 'call_action', fake_send):
                restore_guard = install_outbound_guard(plugin, plugin._reviewer)
                restore_work = install_work_runtime(plugin)
                consecutive_failures = 0
                for i in range(args.start_case, args.start_case + args.count):
                    text = PROMPTS[i % len(PROMPTS)]
                    group = i % 2 == 0
                    origin = 'aiocqhttp:GroupMessage:100' if group else 'aiocqhttp:FriendMessage:200'
                    event = Event(text, origin, '200')
                    event.message_obj.message_id = 'probe-'+str(i)
                    event.set_extra('qqbot_received_at', time.monotonic())
                    event.set_extra('qqbot_meme_recent', [{'text':'今天忙完了，来聊两句', 'time':time.time()}])
                    event.get_self_id = lambda:'99999'
                    event.get_messages = lambda text=text:[Plain(text)]
                    async def event_send(message, *, is_group=group):
                        segments = [{'type':'text','data':{'text':p.text}} for p in message.chain if isinstance(p,Plain)]
                        return await bot.call_action('send_group_msg' if is_group else 'send_private_msg',
                            **({'group_id':'100'} if is_group else {'user_id':'200'}), message=segments)
                    event.send = event_send
                    row = {'case':i+1, 'chat_kind':'group' if group else 'private', 'synthetic_prompt':text}
                    begin = time.monotonic()
                    review_before = plugin._reviewer.stats.get('model_calls',0)
                    delivered_before = plugin._work.status()['reply_text_deliveries']
                    try:
                        async with asyncio.timeout(args.timeout):
                            with plugin._work.reply_work(event):
                                bind_request(origin, ['今天忙完了，来聊两句',text], False)
                                plugin.character_context(event)
                                await image_plugin._start_parallel_proactive_emoji(event, text)
                                req = SimpleNamespace(prompt=text, system_prompt=persona)
                                await plugin.enrich_conversation(event, req)
                                contexts = [{'role':'user','content':'今天忙完了，来聊两句'},
                                            {'role':'assistant','content':'来吧，本龙刚好闲着'},
                                            {'role':'user','content':'刚回到家，准备歇一会儿'},
                                            {'role':'assistant','content':'这班算是让你上明白了，快瘫着吧'}]
                                response = await provider.text_chat(prompt=req.prompt, system_prompt=req.system_prompt, contexts=contexts,
                                                                    func_tool=tools, request_max_retries=1)
                                if response.tools_call_name:
                                    row['tool_requested'] = list(response.tools_call_name)
                                    raise RuntimeError('External tool request: excluded from ordinary complete-text success')
                                history = SimpleNamespace(messages=[Message(role='user',content=text),Message(role='assistant',content=[TextPart(text=response.completion_text)])])
                                await plugin.prepare_chat_punctuation(event, history, response)
                                await image_plugin._prefetch_reply_emoji(event, response)
                                await plugin.review_agent_reply(event, history, response)
                                if event.is_stopped() or not response.completion_text.strip():
                                    raise RuntimeError('Review rejected the output')
                                event.result = MessageEventResult(chain=response.result_chain.chain if response.result_chain else [Plain(response.completion_text)])
                                event.result.set_result_content_type(ResultContentType.LLM_RESULT)
                                await image_plugin._maybe_append_proactive_emoji(event)
                                await event.send(event.result)
                                elapsed = round((time.monotonic()-begin)*1000)
                                await SmartImageSenderPlugin.after_message_sent(image_plugin,event)
                                await plugin.record_reply_latency(event)
                                row.update(ok=True, elapsed_ms=elapsed, reply_chars=len(response.completion_text),
                                           full_review_calls=plugin._reviewer.stats.get('model_calls',0)-review_before,
                                           delivery_metric=plugin._work.status()['reply_text_deliveries']-delivered_before,
                                           prediction_done=bool(plugin.reply_prediction(event)))
                    except Exception as exc:
                        row.update(ok=False, error=type(exc).__name__, reason=str(exc)[:160], elapsed_ms=round((time.monotonic()-begin)*1000))
                    finally:
                        cleanup = event.get_extra('qqbot_cancel_reply_preparation')
                        if callable(cleanup): await cleanup()
                        await plugin._close_reply_pipeline()
                    report['samples'].append(row)
                    consecutive_failures = 0 if row.get('ok') else consecutive_failures + 1
                    report['timings'] = plugin._latency_summary()
                    report['prediction'] = dict(plugin._prediction_stats)
                    report['work'] = plugin._work.status()
                    save()
                    print(json.dumps({k:v for k,v in row.items() if k != 'synthetic_prompt'},ensure_ascii=False),flush=True)
                    if consecutive_failures >= args.max_consecutive_failures:
                        report['stopped_early'] = 'consecutive_model_failures'
                        save()
                        break
        finally:
            restore_work(); restore_guard()
            await image_plugin._terminate_latency_emoji()
            await plugin._learning.close()
            await plugin._close_reply_pipeline()
            await plugin._work.close()
            await provider.client.close()
    print(json.dumps(report['summary'],ensure_ascii=False),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count',type=int,default=100)
    parser.add_argument('--start-case',type=int,default=0)
    parser.add_argument('--max-consecutive-failures',type=int,default=3)
    parser.add_argument('--timeout',type=float,default=90)
    parser.add_argument('--provider',default='')
    parser.add_argument('--persona',default='龙娘')
    parser.add_argument('--hedge-after',type=float,default=15)
    parser.add_argument('--report',default='logs/reply-pipeline-live-benchmark.json')
    asyncio.run(run(parser.parse_args()))
