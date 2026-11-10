import contextlib,importlib.util,json,io,tempfile,unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('reader_bridge_setup',Path(__file__).resolve().parents[1]/'tools/configure_reader_bridge.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class Setup(unittest.TestCase):
    def test_scoped_config_reuses_secret_without_printing_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);data=root/'data';extension=root/'extension';data.mkdir();extension.mkdir()
            (data/'cmd_config.json').write_text('{}');(extension/'manifest.json').write_text('{}');output=io.StringIO()
            with contextlib.redirect_stdout(output):path,local=module.configure(data,'a'*32,extension)
            first=json.loads(path.read_text());self.assertGreaterEqual(len(first['token']),40)
            self.assertEqual(first['extension_origin'],'chrome-extension://'+'a'*32)
            self.assertIn(first['token'],local.read_text());self.assertNotIn(first['token'],output.getvalue())
            module.configure(data,'a'*32,extension);self.assertEqual(first['token'],json.loads(path.read_text())['token'])
    def test_rejects_unknown_directory_and_invalid_extension_id(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):module.configure(folder,'a'*32,folder)
            with self.assertRaises(ValueError):module.configure(folder,'https://example.org',folder)

if __name__=='__main__':unittest.main()
