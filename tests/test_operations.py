import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import urllib.error

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('public_manager_test',ROOT/'manage.py')
manage=importlib.util.module_from_spec(spec);spec.loader.exec_module(manage)
from test_deployment import bootstrap

class PortableOperations(unittest.TestCase):
    def test_check_missing_settings_creates_nothing(self):
        with tempfile.TemporaryDirectory() as d,patch.object(bootstrap,'ROOT',Path(d)),patch('sys.argv',['bootstrap','--check']):
            with self.assertRaises(SystemExit):bootstrap.main()
            self.assertEqual(list(Path(d).iterdir()),[])

    def test_auth_failure_is_diagnostic_and_not_restartable(self):
        with tempfile.TemporaryDirectory() as d,patch.object(manage,'LOGS',Path(d)),patch.object(manage,'api',side_effect=urllib.error.HTTPError('http://localhost',401,'Unauthorized',{},None)):
            report=manage.check()
        self.assertFalse(report['ok']);self.assertFalse(report['checks'][0]['repairable'])

    def test_failed_diagnostic_does_not_make_check_raise(self):
        with patch.object(manage,'api',return_value={'data':{'checks':{'ready':True},'loop_running':True}}),patch.object(manage,'diagnostic',return_value=False):
            self.assertTrue(manage.check()['ok'])

    def test_container_supervisor_identity_is_command_agnostic(self):
        with tempfile.TemporaryDirectory() as d,patch.object(manage,'LOGS',Path(d)):
            record={'command':['python','container_entry.py']};(Path(d)/'supervisor.json').write_text(json.dumps(record))
            with patch.object(manage,'owned',return_value=SimpleNamespace(cmdline=lambda:record['command'])):
                self.assertTrue(manage.running())

    def test_start_waits_for_stopping_instance_before_new_spawn(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);logs=root/'logs';logs.mkdir();(root/'AstrBot/data').mkdir(parents=True)
            (root/'AstrBot/data/cmd_config.json').write_text('{}');(logs/'stopped').touch()
            with patch.object(manage,'ROOT',root),patch.object(manage,'LOGS',logs),patch.object(manage,'running',side_effect=[True,True,False,False,False]),\
                 patch.object(manage.time,'sleep'),patch.object(manage.subprocess,'Popen') as spawn:
                manage.start()
            spawn.assert_called_once();self.assertFalse((logs/'stopped').exists())

    def test_reused_pid_never_stops_unrelated_process(self):
        child=Mock(pid=42,_qqbot_identity={'pid':42})
        with patch.object(manage,'owned',return_value=None),patch.object(manage,'api') as api:
            manage.stop_child(child)
        api.assert_not_called();child.terminate.assert_not_called()

    def test_missing_or_negative_durable_proof_never_terminates(self):
        child=Mock(pid=42,_qqbot_identity={'pid':42})
        for response in ({},{'status':'ok','data':{'drained':True}},
            {'status':'ok','data':{'drained':True,'durable':True,'pending_receipts':1}},
            {'status':'error','data':{'drained':False,'durable':False}}):
            with patch.object(manage,'owned',return_value=child),patch.object(manage,'api',return_value=response):
                with self.assertRaises(manage.UnsafeShutdown):manage.stop_child(child)
        child.terminate.assert_not_called()

    def test_unreachable_drain_does_not_kill_owned_process(self):
        child=Mock(pid=42,_qqbot_identity={'pid':42})
        with patch.object(manage,'owned',return_value=child),patch.object(manage,'api',side_effect=TimeoutError):
            with self.assertRaises(manage.UnsafeShutdown):manage.stop_child(child)
        child.terminate.assert_not_called()

    def test_confirmed_durable_drain_allows_stop(self):
        child=Mock(pid=42,_qqbot_identity={'pid':42});parent=Mock();parent.children.return_value=[]
        with tempfile.TemporaryDirectory() as d,patch.object(manage,'LOGS',Path(d)),patch.object(manage,'owned',return_value=parent),\
            patch.object(manage,'api',return_value={'status':'ok','data':{'drained':True,'durable':True,'pending_persistence':0,'pending_receipts':0}}),\
            patch('psutil.Process',return_value=parent),patch('psutil.wait_procs',return_value=([],[])):
            manage.stop_child(child)
        child.terminate.assert_called_once()

    def test_drain_only_validates_proof_without_signalling_processes(self):
        data={'drained':True,'durable':True,'pending_receipts':0,'pending_persistence':0}
        with patch.object(manage,'api',return_value={'status':'ok','data':data}),patch('psutil.Process') as process:
            self.assertEqual(manage.drain(),data)
        process.assert_not_called()
