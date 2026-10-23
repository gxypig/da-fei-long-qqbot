"""Repeatable Python 3.12 setup. Never imports or copies an existing user's data."""
import argparse
import copy
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent


def validate(settings):
    from urllib.parse import urlparse
    for name in ('api_base', 'api_key', 'model', 'admin_qq', 'dashboard_password'):
        if not isinstance(settings.get(name), str) or not settings[name].strip() or 'YOUR_' in settings[name]:
            raise ValueError('Please fill settings: ' + name)
    if not settings['admin_qq'].isdigit():
        raise ValueError('admin_qq must be your personal numeric QQ ID, not the bot ID')
    url = urlparse(settings['api_base'])
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
        raise ValueError('api_base must be a valid http(s) API base without credentials in URL')
    password = settings['dashboard_password']
    if len(password) < 12 or not any(x.islower() for x in password) or not any(x.isupper() for x in password) or not any(x.isdigit() for x in password):
        raise ValueError('Use a dashboard password of 12+ characters with upper/lowercase and digits')
    if settings.get('review_enabled', False) not in (True, False):
        raise ValueError('review_enabled must be a boolean')


def configure(settings, *, root=ROOT, container=False):
    validate(settings)
    data = root / 'AstrBot/data'
    config_file = data / 'cmd_config.json'
    if config_file.exists():
        raise FileExistsError('Configuration already exists; use AstrBot dashboard to edit it. No user data overwritten.')
    data.mkdir(parents=True, exist_ok=True)
    # AstrBot resolves defaults relative to cwd at import time.
    previous = Path.cwd()
    os.chdir(root / 'AstrBot')
    try:
        from astrbot.core.config.default import DEFAULT_CONFIG
        from astrbot.core.utils.auth_password import hash_dashboard_password
        config = copy.deepcopy(DEFAULT_CONFIG)
        config['admins_id'] = [settings['admin_qq']]
        config['wake_prefix'] = ['/']
        config['provider_sources'] = [{
            'id': 'api', 'provider': 'openai', 'type': 'openai_chat_completion',
            'provider_type': 'chat_completion', 'key': [settings['api_key']],
            'api_base': settings['api_base'].rstrip('/'), 'timeout': 120,
            'proxy': '', 'custom_headers': {}, 'max_context_tokens': 32768,
        }]
        config['provider'] = [{'id': 'chat', 'enable': True, 'model': settings['model'],
                               'provider_source_id': 'api', 'modalities': ['text', 'image'], 'custom_extra_body': {}}]
        vision = settings.get('vision_model') or settings['model']
        if vision != settings['model']:
            config['provider'].append({'id': 'vision', 'enable': True, 'model': vision,
                'provider_source_id': 'api', 'modalities': ['text', 'image'], 'custom_extra_body': {}})
        vision_id = 'vision' if vision != settings['model'] else 'chat'
        config['provider_settings'].update(default_image_caption_provider_id=vision_id,
            streaming_response=False, display_reasoning_text=False,
            web_search=bool(settings.get('search_api_key')), websearch_provider='tavily',
            websearch_tavily_key=[settings['search_api_key']] if settings.get('search_api_key') else [])
        config['platform'] = [{'id': 'aiocqhttp', 'type': 'aiocqhttp', 'enable': True,
            'ws_reverse_host': '0.0.0.0' if container else '127.0.0.1',
            'ws_reverse_port': 6199, 'ws_reverse_token': settings.get('onebot_token') or secrets.token_urlsafe(32)}]
        config['dashboard'].update(enable=True, username='admin', password='',
            pbkdf2_password=hash_dashboard_password(settings['dashboard_password']),
            password_storage_upgraded=True, password_change_required=False,
            jwt_secret=secrets.token_urlsafe(48), host='0.0.0.0' if container else '127.0.0.1', port=6185)
        config['dashboard']['trust_proxy_headers'] = False
        if isinstance(config['agent_runner'].get('config'), dict):
            config['agent_runner']['config'].setdefault('model', {})['provider_id'] = 'chat'
        config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        if os.name != 'nt': config_file.chmod(0o600)
        directory = data / 'config'; directory.mkdir(exist_ok=True)
        for name in ('astrbot_plugin_proactive_agent', 'astrbot_plugin_smart_imagechat_hub', 'astrbot_plugin_bili_scroll'):
            schema = json.loads((data/'plugins'/name/'_conf_schema.json').read_text(encoding='utf-8'))
            def defaults(fields):
                return {k: defaults(v['items']) if v.get('type') == 'object' and isinstance(v.get('items'),dict)
                        else copy.deepcopy(v.get('default')) for k,v in fields.items()}
            values = defaults(schema)
            if name.endswith('proactive_agent'):
                values.update(enable=False, allow_group=False, allow_private=False, allow_qzone=False,
                    reply_review_enabled=bool(settings.get('review_enabled', False)), meme_search_provider='tavily')
            elif name.endswith('smart_imagechat_hub'):
                values['default_image_caption_provider_id'] = vision_id
            target = directory/(name+'_config.json')
            target.write_text(json.dumps(values,ensure_ascii=False,indent=2),encoding='utf-8')
            if os.name != 'nt': target.chmod(0o600)
    finally:
        os.chdir(previous)
    # The official CLI requires the initialized-workspace marker, not just data/.
    (root / 'AstrBot/.astrbot').touch()
    (data / 'temp').mkdir(exist_ok=True)
    return config_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true', help='Create .venv and install pinned AstrBot')
    parser.add_argument('--settings', default='local/settings.json')
    parser.add_argument('--container', action='store_true')
    parser.add_argument('--check', action='store_true', help='Validate settings only; do not install or write')
    args = parser.parse_args()
    if sys.version_info < (3,12): raise SystemExit('Python 3.12+ required; tested with 3.12')
    settings_path = (ROOT / args.settings).resolve()
    if not settings_path.exists():
        settings_path.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(ROOT/'settings.example.json',settings_path)
        raise SystemExit('Created '+str(settings_path)+'; fill your API details and password, then rerun')
    settings = json.loads(settings_path.read_text(encoding='utf-8-sig'))
    validate(settings)
    if args.check:
        print('Settings valid; no installation or messaging performed'); return
    if args.install:
        runtime = ROOT/'.venv'
        if not runtime.exists(): venv.EnvBuilder(with_pip=True).create(runtime)
        python = runtime/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        subprocess.run([str(python),'-m','pip','install','-r',str(ROOT/'requirements.txt')],check=True)
        command = [str(python),str(Path(__file__).resolve()),'--settings',str(settings_path)]
        if args.container: command.append('--container')
        subprocess.run(command,check=True); return
    configure(settings,container=args.container)
    from tools.repair_dashboard import inspect_view
    result = inspect_view(repair=True)
    print('AstrBot configured; plugins bundled; outbound review:',bool(settings.get('review_enabled')))
    print('Dashboard compatibility:',result.get('ok'),result.get('detail'))
    print('Start: python manage.py start  |  Dashboard: http://127.0.0.1:6185 (user admin)')
    print('Read local OneBot token from AstrBot/data/cmd_config.json; do not share it')


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        # Never dump settings, credentials or raw remote response bodies.
        raise SystemExit(f'{type(exc).__name__}: {exc}')
