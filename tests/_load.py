"""Load a script from a skill folder (the folder names have hyphens)."""

import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")


def load(folder, name):
    path = os.path.join(ROOT, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("trader_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses resolves annotations via
    # sys.modules[cls.__module__], which is None for an unregistered module
    # once `from __future__ import annotations` makes them strings.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod
