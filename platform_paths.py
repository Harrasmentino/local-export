"""Private application-data locations for supported desktop systems."""
import os
import sys
from pathlib import Path


def app_data_dir() -> Path:
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'ConfluenceLocalExport'
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/ConfluenceLocalExport'
    base = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share')))
    return base / 'ConfluenceLocalExport'