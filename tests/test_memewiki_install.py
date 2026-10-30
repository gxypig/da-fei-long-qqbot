from contextlib import closing
import hashlib
import gzip,json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools import install_memewiki as installer


class DictionaryInstall(unittest.TestCase):
    def test_catalog_pages_are_complete_and_missing_page_never_publishes(self):
        import io
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            pages=[{'items':[{'title':'示例一','slug':'one','summary':'含义','tags':[]}],'offset':0,'has_more':True},
                   {'items':[{'title':'示例二','slug':'two','summary':'含义','tags':[]}],'offset':1,'has_more':False}]
            with patch.object(installer,'urlopen',side_effect=[io.BytesIO(json.dumps(p).encode()) for p in pages]),patch.object(installer.time,'sleep'):
                result=installer.install(root)
            self.assertEqual(result['entries'],2);self.assertEqual(result['revision'],'public-api-catalog')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            pages=[pages[0],{'items':[],'offset':1,'has_more':True}]
            with patch.object(installer,'urlopen',side_effect=[io.BytesIO(json.dumps(p).encode()) for p in pages]),patch.object(installer.time,'sleep'):
                with self.assertRaisesRegex(ValueError,'Incomplete'):installer.install(root)
            self.assertFalse((root/installer.RELATIVE).exists())

    def test_gzip_offline_uses_uncompressed_trusted_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source.db'
            with closing(sqlite3.connect(source)) as db,db:
                db.execute('CREATE TABLE memes(title TEXT,slug TEXT,summary TEXT,usage TEXT,origin TEXT,other TEXT)')
                db.execute("INSERT INTO memes VALUES('示例','one','解释','场景','','')")
            data=source.read_bytes();packed=root/'source.db.gz';packed.write_bytes(gzip.compress(data))
            result=installer.install(root,packed,hashlib.sha256(data).hexdigest())
            self.assertEqual(result['entries'],1);self.assertEqual((root/installer.RELATIVE).read_bytes(),data)

    def test_checksum_validation_atomic_install_and_preserve_existing(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source.db'
            with closing(sqlite3.connect(source)) as db,db:
                db.execute('CREATE TABLE memes(title TEXT,slug TEXT,summary TEXT,usage TEXT,origin TEXT,other TEXT)')
                db.execute("INSERT INTO memes VALUES ('示例','example','定义','用法','出处','')")
            target=root/installer.RELATIVE
            with self.assertRaisesRegex(ValueError,'SHA256'):
                installer.install(root,source)
            self.assertFalse(target.exists())
            with patch.object(installer,'SHA256',hashlib.sha256(source.read_bytes()).hexdigest()):
                result=installer.install(root,source)
            self.assertEqual(result['entries'],1)
            self.assertEqual(target.read_bytes(),source.read_bytes())
            with patch.object(installer,'urlopen',side_effect=AssertionError('no network on restart')):
                self.assertEqual(installer.install(root)['status'],'preserved_existing')
            self.assertTrue((target.parent/'source.json').exists())

    def test_invalid_existing_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target=root/installer.RELATIVE
            target.parent.mkdir(parents=True);target.write_bytes(b'broken')
            with self.assertRaises(sqlite3.Error):installer.install(root)
            self.assertEqual(target.read_bytes(),b'broken')
