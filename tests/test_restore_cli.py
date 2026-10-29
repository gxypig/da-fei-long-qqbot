import importlib.util,json,sqlite3,tempfile,unittest
from pathlib import Path
from contextlib import closing
ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
backup=load('backup_cli_fixture',ROOT/'AstrBot/data/plugins/astrbot_plugin_proactive_agent/data_backup.py')
cli=load('restore_cli_fixture',ROOT/'tools/restore_data_snapshot.py')

class RestoreCLI(unittest.TestCase):
    def test_roundtrip_checks_sqlite_and_refuses_existing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'data';source.mkdir()
            with closing(sqlite3.connect(source/'records.sqlite3')) as db:
                db.execute('CREATE TABLE facts(value TEXT)');db.execute("INSERT INTO facts VALUES('confirmed')");db.commit()
            (source/'settings.json').write_text('{}')
            result=backup.snapshot(source,root/'backup')
            manifest=root/'backup/snapshots'/result['snapshot']/'manifest.json'
            restored=cli.restore(manifest,root/'restored')
            self.assertEqual(restored['sqlite_verified'],1);self.assertFalse(restored['replayed'])
            with closing(sqlite3.connect(root/'restored/records.sqlite3')) as db:self.assertEqual(db.execute('SELECT value FROM facts').fetchone()[0],'confirmed')
            with self.assertRaises(FileExistsError):cli.restore(manifest,root/'restored')

    def test_corrupt_shared_content_stops_before_destination_created(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'data';source.mkdir();(source/'settings.json').write_text('{}')
            result=backup.snapshot(source,root/'backup');manifest=root/'backup/snapshots'/result['snapshot']/'manifest.json'
            metadata=json.loads(manifest.read_text());digest=metadata['files']['settings.json']['sha256']
            (root/'backup/blobs'/digest).write_text('corrupted')
            with self.assertRaises(ValueError):cli.restore(manifest,root/'new')
            self.assertFalse((root/'new').exists())
