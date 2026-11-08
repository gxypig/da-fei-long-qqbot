import os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import temp_cleanup as cleanup

@unittest.skipUnless(os.name=='nt','Windows delete-pending behavior')
class TemporaryCleanup(unittest.TestCase):
    def test_empty_failed_child_can_retry_with_untouched_sibling_files(self):
        original=cleanup.OriginalTemporaryDirectory.cleanup
        with cleanup.OriginalTemporaryDirectory() as folder:
            root=Path(folder);child=root/'empty';child.mkdir();(root/'sibling').write_text('untouched')
            obj=cleanup.SettledTemporaryDirectory.__new__(cleanup.SettledTemporaryDirectory);obj.name=folder
            error=OSError(39,'delete pending',str(child));error.winerror=145
            with patch.object(cleanup.OriginalTemporaryDirectory,'cleanup',side_effect=[error,None]) as call:
                obj.cleanup();self.assertEqual(call.call_count,2)
            self.assertTrue((root/'sibling').is_file())
    def test_live_files_in_failed_child_are_never_ignored(self):
        with cleanup.OriginalTemporaryDirectory() as folder:
            root=Path(folder);child=root/'live';child.mkdir();(child/'active').write_text('still writing')
            obj=cleanup.SettledTemporaryDirectory.__new__(cleanup.SettledTemporaryDirectory);obj.name=folder
            error=OSError(39,'delete pending',str(child));error.winerror=145
            with patch.object(cleanup.OriginalTemporaryDirectory,'cleanup',side_effect=error) as call:
                with self.assertRaises(OSError):obj.cleanup()
                self.assertEqual(call.call_count,1)
