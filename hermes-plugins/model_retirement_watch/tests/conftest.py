import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "model_retirement_watch", PLUGIN_DIR / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["model_retirement_watch"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return load_plugin_module()
