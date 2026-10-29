"""Read-only index of editable character prompts; never export config values."""
import importlib.util
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
AGENT = 'AstrBot/data/plugins/astrbot_plugin_proactive_agent/'
SOURCES = {
    'dragon_character.py': '可信关系、停止玩笑与表达检查',
    'persona_registry.py': '人格整理、场景适配与独立校验协议',
    'persona_sources.py': '人格来源清单与来源优先级',
    'cheeky_tone.py': '毒舌程度、具体口吻与示例',
    'persona_authority.py': '当前人格与插件补充的优先关系',
    'chat_style_policy.py': '闲聊、身份与知识请求的回应方式',
    'conversation_style.py': '日常聊天补充',
    'conversation_review.py': '发送前审阅与改写要求',
    'reply_pipeline.py': '并行预判和表情风格',
    'local_recall.py': '短回复与自然用梗',
    'chat_punctuation.py': '标点习惯',
    'visual_intent.py': '自拍、形象和图片回应',
    'idle_learning.py': '学习表达策略及示例',
    'life_state.py': '主动生活及统一角色组装',
    'proactive_policy.py': '自由主动说话',
    'fixed_schedule.py': '定时消息与动态',
    'proactive_variety.py': '主动话题与差异',
    'main.py': '最终聊天提示词组装入口',
    '_conf_schema.json': '初次安装默认值；不覆盖已保存的配置',
}
RELATED = {
    'astrbot_plugin_bili_scroll/main.py': 'B站分享与评论口吻',
    'astrbot_plugin_smart_imagechat_hub/backend/retrieval.py': '找图回应与表情选择',
    'astrbot_plugin_smart_imagechat_hub/backend/proactive_fast_retrieval.py': '快速表情语气',
}
FRAMEWORK = {
    'core/persona_mgr.py': '框架默认人格；现有保存值在面板编辑',
    'core/astr_main_agent_resources.py': '框架各场景默认提示',
    'core/astr_main_agent.py': '框架人格、额外提示与技能组装',
    'core/config/default.py': '框架初始默认提示；现有配置在面板编辑',
}


def build_plan(root=ROOT, framework=None):
    root = Path(root).resolve()
    items, seen, warnings = [], set(), []

    def add(path, label, required=False):
        path = Path(path).resolve()
        if path.is_file() and str(path).casefold() not in seen:
            seen.add(str(path).casefold())
            items.append({'path': str(path), 'label': label})
        elif required and not path.is_file():
            warnings.append('缺少文件：' + str(path))

    add(root/'性格提示词修改指南.md', '先阅读：入口、优先级和生效方式', True)
    for name, label in SOURCES.items():
        add(root/AGENT/name, label, True)
    plugins = root/'AstrBot/data/plugins'
    for name, label in RELATED.items():
        add(plugins/name, label, True)
    # Include new personality-bearing source files, without opening runtime data.
    if plugins.is_dir():
        for current, dirs, files in os.walk(plugins):
            dirs[:] = [d for d in dirs if d not in {'.git', '__pycache__', 'data', 'imgs', 'node_modules', '.astrbot-plugin'}]
            for name in sorted(files):
                if name in {'SKILL.md', 'EXTRA_PROMPT.md'}:
                    add(Path(current)/name, '插件提供的技能或额外提示')
                if not (name.endswith('.py') or name == '_conf_schema.json'):
                    continue
                path = Path(current)/name
                try:
                    text = path.read_text('utf-8-sig', errors='replace')
                except OSError:
                    warnings.append('无法读取源码：' + str(path))
                    continue
                if re.search(r'性格|口吻|人设|人格|被怼|道歉|嘴欠', text):
                    add(path, '补充发现：含角色或表达规则，先查看具体用途')
    if framework is None:
        spec = importlib.util.find_spec('astrbot')
        framework = Path(spec.origin).parent if spec and spec.origin else None
    if framework:
        for name, label in FRAMEWORK.items():
            add(Path(framework)/name, label, True)
    else:
        warnings.append('未找到AstrBot框架目录；安装后再打开可补齐框架默认提示')

    # Session/project workspaces may be outside this checkout. Read only their
    # stored paths, never message bodies, persona prose, tokens or credentials.
    workspaces = root/'AstrBot/data/workspaces'
    locations = {root, root/'AstrBot', workspaces, root/'AstrBot/data/skills'}
    database = root/'AstrBot/data/data_v4.db'
    if database.is_file():
        try:
            with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=2)) as db:
                tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                for table in tables:
                    quoted = '"'+table.replace('"', '""')+'"'
                    if 'workspace_path' not in {r[1] for r in db.execute('PRAGMA table_info('+quoted+')')}:
                        continue
                    for row in db.execute('SELECT DISTINCT workspace_path FROM '+quoted+' WHERE workspace_path IS NOT NULL'):
                        value = str(row[0]).strip()
                        if value:
                            path = Path(value).expanduser()
                            locations.add(path if path.is_absolute() else workspaces/path)
        except sqlite3.Error:
            warnings.append('工作区位置读取失败；当前项目入口仍可打开，稍后重试以补齐自定义工作区')
    for location in sorted(locations, key=str):
        add(location/'EXTRA_PROMPT.md', '会话/项目额外提示')
        # Scan designated workspace/skill trees only, never the whole checkout.
        scan = [location] if location in {workspaces, root/'AstrBot/data/skills'} else [location/'.astrbot/skills', location/'skills']
        for directory in scan:
            if not directory.is_dir():
                continue
            for current, dirs, files in os.walk(directory):
                dirs[:] = [d for d in dirs if d not in {'.git', 'node_modules', '__pycache__', '.venv', 'media', 'images'}]
                for name in ('EXTRA_PROMPT.md', 'SKILL.md'):
                    if name in files:
                        add(Path(current)/name, '额外提示/技能（是否生效由会话和技能开关决定）')

    port = 6185
    config = root/'AstrBot/data/cmd_config.json'
    if config.is_file():
        try:
            port = int(json.loads(config.read_text('utf-8-sig')).get('dashboard', {}).get('port', port))
            if not 1 <= port <= 65535:
                raise ValueError()
        except (ValueError, OSError, TypeError):
            port = 6185
            warnings.append('未能读取控制台端口，暂用6185')
    base = f'http://127.0.0.1:{port}/#'
    pages = [
        {'label': '集中人格编辑器：编辑总设定、保存并同步各用途、查看来源及回退', 'url': base+'/plugin-view/astrbot_plugin_proactive_agent/persona'},
        {'label': '已保存的人格：包括数据库中那份旧设定，选择当前会话使用的人格', 'url': base+'/persona'},
        {'label': '插件配置：主动人格personality等保存值', 'url': base+'/extension/plugins/astrbot_plugin_proactive_agent'},
        {'label': '大肥龙面板：龙娘设定、学习与记忆、审阅开关', 'url': base+'/plugin-view/astrbot_plugin_proactive_agent/control'},
        {'label': '框架配置：人格选择、提示前缀、子代理等', 'url': base+'/config'},
        {'label': '技能：查看和修改已启用的技能提示', 'url': base+'/extension/skills'},
        {'label': '图库插件配置：找图与表情表达相关设置', 'url': base+'/extension/plugins/astrbot_plugin_smart_imagechat_hub'},
        {'label': 'B站插件配置：分享与评论表达相关设置', 'url': base+'/extension/plugins/astrbot_plugin_bili_scroll'},
    ]
    return {'root': str(root), 'files': items, 'pages': pages, 'warnings': warnings}


if __name__ == '__main__':
    # ASCII JSON is reliable across Windows PowerShell 5.1 code pages.
    print(json.dumps(build_plan(), ensure_ascii=True))
