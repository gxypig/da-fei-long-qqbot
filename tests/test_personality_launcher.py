import importlib.util
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('personality_locations', ROOT/'tools/personality_locations.py')
locations = importlib.util.module_from_spec(spec)
spec.loader.exec_module(locations)


class PersonalityLauncherTests(unittest.TestCase):
    def test_saved_persona_and_config_are_editable_without_opening_secrets(self):
        plan = locations.build_plan(ROOT)
        self.assertFalse(plan['warnings'], plan['warnings'])
        urls = [p['url'] for p in plan['pages']]
        self.assertTrue(any(u.endswith('/persona') for u in urls))
        self.assertTrue(any(u.endswith('/config') for u in urls))
        self.assertTrue(any('/plugin-view/' in u for u in urls))
        files = [Path(p['path']).name for p in plan['files']]
        self.assertIn('persona_authority.py', files)
        self.assertIn('persona_mgr.py', files)
        self.assertNotIn('cmd_config.json', files)
        self.assertNotIn('data_v4.db', files)
        self.assertEqual(len(plan['files']), len({p['path'] for p in plan['files']}))

    def test_custom_workspace_and_new_role_source_discovery(self):
        with tempfile.TemporaryDirectory(prefix='角色路径 with spaces ') as folder:
            root = Path(folder)/'project'; data = root/'AstrBot/data';data.mkdir(parents=True)
            external = Path(folder)/'separate';external.mkdir()
            (external/'EXTRA_PROMPT.md').write_text('extra', 'utf-8')
            plugin = data/'plugins/new_plugin';plugin.mkdir(parents=True)
            (plugin/'new_role.py').write_text('PROMPT="新的性格"', 'utf-8')
            (data/'cmd_config.json').write_text(json.dumps({'dashboard':{'port':9876,'jwt_secret':'must-not-appear'}}), 'utf-8')
            with closing(sqlite3.connect(data/'data_v4.db')) as db:
                db.execute('CREATE TABLE projects (workspace_path TEXT)')
                db.execute('INSERT INTO projects VALUES (?)', (str(external),))
                db.commit()
            plan = locations.build_plan(root, framework=root/'missing')
            self.assertIn(str((external/'EXTRA_PROMPT.md').resolve()), [p['path'] for p in plan['files']])
            self.assertIn(str((plugin/'new_role.py').resolve()), [p['path'] for p in plan['files']])
            self.assertTrue(all(':9876/' in p['url'] for p in plan['pages']))
            self.assertNotIn('must-not-appear', json.dumps(plan))
            self.assertTrue(plan['warnings'])  # missing sources must be visible

    @unittest.skipUnless(os.name == 'nt', 'Windows launcher')
    def test_check_only_works_from_different_cwd(self):
        with tempfile.TemporaryDirectory(prefix='launcher cwd ') as cwd:
            result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT/'scripts/open-personality-prompts.ps1'), '-CheckOnly'], cwd=cwd, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        plan = json.loads(result.stdout)
        self.assertEqual(Path(plan['root']), ROOT)
        self.assertFalse(plan['warnings'], plan['warnings'])

    @unittest.skipUnless(os.name == 'nt', 'Windows launcher')
    def test_opening_uses_explicit_editor_and_preserves_paths(self):
        # Capture OS launches; never open real editor/browser windows during tests.
        script = str(ROOT/'scripts/open-personality-prompts.ps1').replace("'", "''")
        command = "$global:opened=@(); function Start-Process { param($FilePath,$ArgumentList) $global:opened+=@{file=$FilePath;args=$ArgumentList} }; & '"+script+"'; Write-Output 'JSON_START'; ConvertTo-Json -Depth 6 -InputObject @($global:opened)"
        result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command], capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        # ASCII-safe editor names/URLs suffice; script path quoting is separately checked.
        raw = result.stdout.decode('utf-8', errors='replace').split('JSON_START',1)[-1]
        calls = json.loads(raw)
        editors = [c for c in calls if not c['file'].startswith('http')]
        self.assertTrue(editors)
        self.assertTrue(all(Path(c['file']).name.lower() in {'notepad.exe','code.exe','cursor.exe'} for c in editors))
        pages=[c['file'] for c in calls if c['file'].startswith('http')]
        self.assertEqual(len(pages),8)
        self.assertTrue(any(p.endswith('/plugin-view/astrbot_plugin_proactive_agent/persona') for p in pages))
        for call in editors:
            args = call['args'] if isinstance(call['args'], list) else [call['args']]
            self.assertTrue(all(a=='--new-window' or a.startswith('"') and a.endswith('"') for a in args))
