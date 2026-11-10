import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from test_maintenance import ROOT
from data.plugins.astrbot_plugin_proactive_agent import data_backup as backup

class CleanupRace(unittest.TestCase):
    def test_windows_nonempty_directory_cleanup_is_bounded_and_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);owned=root/'snapshot';owned.mkdir()
            (owned/'manifest.json').write_text(json.dumps({'format':'qqbot-data-snapshot-v1'}),'utf-8')
            original=backup.shutil.rmtree;calls=[]
            def cleanup(path,**kwargs):
                calls.append(path)
                if len(calls)==1:
                    error=OSError('directory briefly nonempty');error.winerror=145;raise error
                return original(path,**kwargs)
            with patch.object(backup.shutil,'rmtree',side_effect=cleanup),patch.object(backup.time,'sleep'):
                backup.remove_snapshot(owned,root)
            self.assertEqual(len(calls),2);self.assertFalse(owned.exists())
    def test_disappearing_sidecar_is_ignored_but_other_errors_propagate(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);owned=root/'snapshot';owned.mkdir()
            (owned/'manifest.json').write_text(json.dumps({'format':'qqbot-data-snapshot-v1'}),'utf-8')
            def cleanup(path,*,onexc):
                onexc(None,str(path/'db-shm.tmp'),FileNotFoundError('already gone'))
                for filename,error in ((path,FileNotFoundError('root')), (root/'outside',FileNotFoundError('outside')), (path/'db',PermissionError('locked'))):
                    with self.assertRaises(type(error)):onexc(None,str(filename),error)
            with patch.object(backup.shutil,'rmtree',side_effect=cleanup) as removed:
                backup.remove_snapshot(owned,root)
            removed.assert_called_once()

if __name__=='__main__':unittest.main()
