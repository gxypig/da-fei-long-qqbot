import hashlib
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('delivery_migration_fixture',ROOT/'tools/migrate_delivery_history.py')
migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)

class Migration(unittest.TestCase):
    def test_apply_twice_keeps_original_chat_and_invalidates_only_affected_summary(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);folder=root/'AstrBot/data/plugin_data/astrbot_plugin_proactive_agent';folder.mkdir(parents=True)
            original=root/'AstrBot/data/data_v4.db';original.write_bytes(b'original user + tool + draft data')
            before=hashlib.sha256(original.read_bytes()).hexdigest()
            cache=folder/'context-summaries.json';cache.write_text(json.dumps({migration.digest('affected'):{'text':'unsent'},migration.digest('unaffected'):{'text':'keep'}}))
            item={'cid':'affected','origin':'p:FriendMessage:123','position':3,'row_hash':'exact','previous_hash':'previous','text_hash':'text'}
            self.assertEqual(migration.apply(root,{'candidates':[item]}),1)
            self.assertEqual(migration.apply(root,{'candidates':[item]}),0)
            with closing(sqlite3.connect(folder/'delivery-ledger.sqlite3')) as db:
                rows=db.execute('SELECT body FROM records').fetchall()
            self.assertEqual(len(rows),1);self.assertEqual(json.loads(rows[0][0])['state'],'legacy_undelivered')
            self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(),before)
            self.assertEqual(json.loads(cache.read_text()),{migration.digest('unaffected'):{'text':'keep'}})
