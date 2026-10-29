"""Windows can briefly report an empty delete-pending directory as nonempty.

Retry only that verified-empty state; never ignore live files or failed writes.
All worker shutdown assertions still run before temporary cleanup.
"""
import os,tempfile,time
from pathlib import Path
OriginalTemporaryDirectory=tempfile.TemporaryDirectory

class SettledTemporaryDirectory(OriginalTemporaryDirectory):
    def cleanup(self):
        for attempt in range(6):
            try:return super().cleanup()
            except OSError as exc:
                root=Path(self.name)
                if os.name!='nt' or getattr(exc,'winerror',None)!=145 or attempt==5:raise
                try:
                    if root.exists() and any(root.iterdir()):raise exc
                except FileNotFoundError:return
                time.sleep(.02)

def install():
    if os.name=='nt':tempfile.TemporaryDirectory=SettledTemporaryDirectory
