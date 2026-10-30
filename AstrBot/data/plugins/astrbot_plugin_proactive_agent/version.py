"""Runtime version comes from the installed plugin metadata, not copied labels."""
import re
from pathlib import Path
_text=Path(__file__).with_name('metadata.yaml').read_text('utf-8')
VERSION=next(iter(re.findall(r'^version:\s*[\"\x27]?([^\s\"\x27]+)',_text,re.M)),'unknown')
