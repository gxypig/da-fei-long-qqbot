"""Fixed bundled views within AstrBot's existing authenticated, sandboxed bridge."""
import asyncio,re
from pathlib import Path
from quart import request, jsonify

VIEWS = {'persona': ('persona_state', 'persona_action'), 'sessions': ('mute_state', 'mute_action')}

def bundled_view(name):
    if name not in VIEWS:
        raise ValueError('未知管理页面')
    root = Path(__file__).parent / 'pages' / name
    html = (root / 'index.html').read_text('utf-8')
    style = root.parent/'persona'/'style.css' if name == 'sessions' else root/'style.css'
    css = style.read_text('utf-8').replace('</style', '<\\/style')
    js = (root / 'app.js').read_text('utf-8').replace('</script', '<\\/script')
    style_href = '../persona/style.css' if name == 'sessions' else './style.css'
    html=re.sub(r'<link rel="stylesheet" href="'+re.escape(style_href)+r'(?:\?[^"<>]*)?">',lambda _: '<style>'+css+'</style>',html)
    html=html.replace('<script src="/api/plugin/page/bridge-sdk.js"></script>', '')
    return re.sub(r'<script src="\./app\.js(?:\?[^"<>]*)?"></script>',lambda _: '<script>'+js+'</script>',html)

def register_subview_panel(plugin):
    async def view():
        name = request.args.get('view')
        try:
            html = await asyncio.to_thread(bundled_view, name)
            return jsonify(status='ok', data={'html': html, 'endpoints': VIEWS[name]})
        except ValueError as exc:
            return jsonify(status='error', message=str(exc)), 400
    plugin.context.register_web_api('/astrbot_plugin_proactive_agent/panel_subview', view, ['GET'], '独立人格与会话管理页面')
