"""Seed bundled plugins into the persistent volume, then start the supervisor."""
from pathlib import Path
import json
import subprocess
import sys
import shutil
from bootstrap import configure

root=Path(__file__).resolve().parent
# Refresh only shipped plugin source; runtime data stays in the persistent volume.
for source in (root/'bundled_plugins').iterdir():
    if source.is_dir():
        shutil.copytree(source,root/'AstrBot/data/plugins'/source.name,dirs_exist_ok=True)
config=root/'AstrBot/data/cmd_config.json'
if not config.exists():
    configure(json.loads((root/'local/settings.json').read_text(encoding='utf-8-sig')),container=True)
from tools.repair_dashboard import inspect_view
print(inspect_view(repair=True))
# Foreground supervisor gives Docker ownership of the whole process group.
from manage import supervise, LOGS
LOGS.mkdir(exist_ok=True)
(LOGS/'stopped').unlink(missing_ok=True)
supervise()
