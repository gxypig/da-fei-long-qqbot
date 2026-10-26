"""Bounded fixture cleanup for Windows' transient delete-pending directories.

Runtime worker joins are tested separately; persistent errors still fail tests.
Only the TemporaryDirectory's own generated path can be removed here.
"""
import tempfile
import time

class TemporaryDirectory(tempfile.TemporaryDirectory):
    def cleanup(self):
        for attempt in range(6):
            try:return super().cleanup()
            except OSError as exc:
                if getattr(exc,'winerror',None) not in {5,32,33,145} or attempt==5:raise
                time.sleep(.01*2**attempt)
