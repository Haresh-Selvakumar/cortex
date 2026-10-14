"""Installed-package defaults; explicit configuration paths are never silently replaced."""
from importlib.resources import files
import json
from pathlib import Path


def load_config(path,default_name):
    if path is not None:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    local=Path('config')/default_name
    if local.is_file():return json.loads(local.read_text(encoding='utf-8'))
    return json.loads(files('cortex').joinpath('default_config',default_name).read_text(encoding='utf-8'))
