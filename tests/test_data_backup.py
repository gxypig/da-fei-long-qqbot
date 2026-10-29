import json,sqlite3,tempfile,threading,unittest
from contextlib import closing
from pathlib import Path
from test_maintenance import ROOT
from data.plugins.astrbot_plugin_proactive_agent.data_backup import snapshot,restore,SnapshotChanged,retain

class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.source=self.root/'data';self.source.mkdir();self.backups=self.root/'backups'
        (self.source/'settings.json').write_text('{"value":1}','utf-8')
        (self.source/'image.png').write_bytes(b'large-immutable-image')
        self.db=sqlite3.connect(self.source/'records.db');self.addCleanup(self.db.close)
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('CREATE TABLE rows(value TEXT)');self.db.execute("INSERT INTO rows VALUES('confirmed')");self.db.commit()
    def test_backup_restores_sqlite_wal_config_and_assets_without_replaying(self):
        result=snapshot(self.source,self.backups)
        self.assertTrue(result['consistent'])
        target=self.root/'restored';answer=restore(self.backups/'snapshots'/result['snapshot']/'manifest.json',target)
        self.assertFalse(answer['replayed']);self.assertEqual((target/'image.png').read_bytes(),b'large-immutable-image')
        with closing(sqlite3.connect(target/'records.db')) as db:self.assertEqual(db.execute('SELECT value FROM rows').fetchone()[0],'confirmed')
        with self.assertRaises(FileExistsError):restore(self.backups/'snapshots'/result['snapshot']/'manifest.json',target)
    def test_changed_database_or_json_never_publishes_partial_snapshot(self):
        def change():self.db.execute("INSERT INTO rows VALUES('new')");self.db.commit()
        with self.assertRaises(SnapshotChanged):snapshot(self.source,self.backups,hook=change)
        self.assertEqual(list((self.backups/'snapshots').glob('*/manifest.json')),[])
        with self.assertRaises(SnapshotChanged):snapshot(self.source,self.backups,hook=lambda:(self.source/'settings.json').write_text('{"value":2}','utf-8'))
    def test_shared_assets_deduplicate_and_retention_keeps_24_hourly_plus_seven_days(self):
        at=1793000000
        for hour in range(0,8*24,4):snapshot(self.source,self.backups,clock=lambda hour=hour:at+hour*3600)
        manifests=[json.loads(p.read_text('utf-8')) for p in (self.backups/'snapshots').glob('*/manifest.json')]
        self.assertGreaterEqual(len(manifests),24);self.assertLessEqual(len(manifests),31)
        self.assertEqual(len(list((self.backups/'blobs').glob('*'))),2)
        self.assertTrue(all(m['complete'] for m in manifests))
    def test_cancel_yields_to_chat_and_integrity_mismatch_refuses_restore(self):
        cancelled=threading.Event();cancelled.set()
        with self.assertRaises(SnapshotChanged):snapshot(self.source,self.backups,cancel=cancelled)
        result=snapshot(self.source,self.backups);manifest=self.backups/'snapshots'/result['snapshot']/'manifest.json'
        first=next((self.backups/'blobs').iterdir());first.write_bytes(b'corrupt')
        with self.assertRaises(ValueError):restore(manifest,self.root/'restore-bad')
        self.assertFalse((self.root/'restore-bad').exists())
