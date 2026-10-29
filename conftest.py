"""Shared pytest setup for the pwr-tray test suite.

pwr_tray.main imports a GUI toolkit, a session D-Bus binding and a YAML
library at module scope, none of which are needed to test the pure decision
logic (timeout rotation, pick persistence, battery tier selection).  When a
dependency is not installed -- as in a bare CI container -- a permissive stub
is registered so the modules can still be imported.  Real dependencies are
used whenever they are present, so the suite also runs on a developer desktop.

Every test is also given a throw-away HOME: IniTool writes config.ini and
picks.json under ~/.config/pwr-tray, so without isolation the suite would
clobber a real user profile.
"""

import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class _Stub:
    """Absorbs any attribute access or call, for imports we cannot satisfy."""

    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return _Stub()

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        return _Stub()

    def __bool__(self):
        return False


def _available(name):
    """True if `name` is importable without actually importing it here."""
    if name in sys.modules:
        return True
    try:
        __import__(name)
    except Exception:
        return False
    return True


def _module_getattr(name):
    # PEP 562 module hook.  Dunders must raise: answering e.g. __file__ with a
    # stub makes inspect (and therefore pytest's traceback rendering) treat the
    # module as a builtin and fail with TypeError.
    if name.startswith('__'):
        raise AttributeError(name)
    return _Stub()


def _stub(module_name, **attrs):
    """Register `module_name` (and, for dotted names, its parent) as a stub."""
    parts = module_name.split('.')
    for depth in range(1, len(parts)):
        parent = '.'.join(parts[:depth])
        if parent not in sys.modules:
            sys.modules[parent] = types.ModuleType(parent)
    module = types.ModuleType(module_name)
    for key, value in attrs.items():
        setattr(module, key, value)
    module.__getattr__ = _module_getattr
    sys.modules[module_name] = module
    if len(parts) > 1:
        setattr(sys.modules['.'.join(parts[:-1])], parts[-1], module)
    return module


if not _available('PyQt5'):
    _stub('PyQt5.QtCore', QTimer=_Stub)
    _stub('PyQt5.QtGui', QIcon=_Stub, QCursor=_Stub)
    _stub('PyQt5.QtWidgets', QApplication=_Stub, QSystemTrayIcon=_Stub,
          QMenu=_Stub, QAction=_Stub)

if not _available('pydbus'):
    _stub('pydbus', SessionBus=_Stub)

if not _available('ruamel.yaml'):
    _stub('ruamel.yaml', YAML=_Stub)

if not _available('psutil'):
    _stub('psutil', sensors_battery=lambda: None)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point HOME at an empty directory and restore the cwd afterwards."""
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / '.config'))
    cwd = os.getcwd()
    try:
        yield home
    finally:
        os.chdir(cwd)
