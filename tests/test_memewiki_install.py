from contextlib import closing
import hashlib
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
