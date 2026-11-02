"""Shared pytest setup for the pwr-tray test suite.

pwr_tray.main imports a GUI toolkit and a YAML library at module scope, neither
of which is needed to test the pure decision logic (timeout rotation, pick
persistence, battery tier selection).  When a
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
    except ImportError:
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
    # QtMsgType is a namespace of real values rather than a _Stub: the message
    # handler compares levels by identity, and a fresh _Stub() per attribute
    # access would make every message look like a warning.
    _stub('PyQt5.QtCore', QTimer=_Stub,
          QtMsgType=types.SimpleNamespace(
              QtDebugMsg=0, QtInfoMsg=4, QtWarningMsg=1,
              QtCriticalMsg=2, QtFatalMsg=3),
          qInstallMessageHandler=_Stub())
    # QSessionManager needs a real namespace rather than a _Stub: the
    # session-shutdown test compares the restart hint it recorded against the
    # enum, and two fresh _Stub() objects never compare equal.
    _stub('PyQt5.QtGui', QIcon=_Stub, QCursor=_Stub,
          QSessionManager=types.SimpleNamespace(
              RestartHint=types.SimpleNamespace(RestartNever=3),
              RestartNever=3))
    _stub('PyQt5.QtWidgets', QApplication=_Stub, QSystemTrayIcon=_Stub,
          QMenu=_Stub, QAction=_Stub)
    # No QtDBus stub on purpose: tray_watcher_present() imports it lazily and
    # treats its absence as "cannot ask the bus", which is what a CI container
    # should exercise.

if not _available('ruamel.yaml'):
    _stub('ruamel.yaml', YAML=_Stub)

if not _available('psutil'):
    _stub('psutil', sensors_battery=lambda: None)


@pytest.fixture(autouse=True)
def _never_run_real_power_commands(monkeypatch):
    """Tripwire: no automated test may touch the machine's power state.

    The action layer is the only code that starts lockers, blanks monitors or
    calls ``systemctl suspend/poweroff/reboot``.  Replace its ``subprocess``
    with one that refuses to run anything, so a test that forgets to install
    its own recorder fails instead of locking the session or shutting the
    machine down.  Tests that deliberately exercise the action layer install a
    recorder over this (see ``tests/test_actions.py``).

    Real power-state verification stays a manual step by design.
    """
    import subprocess as real_subprocess

    try:
        from pwr_tray import Actions as actions_module
    except ImportError:        # pragma: no cover - package always importable
        yield
        return

    def refuse(*_args, **_kwargs):
        raise AssertionError(
            'test attempted to run a real command through pwr_tray.Actions;'
            ' install a fake subprocess first')

    monkeypatch.setattr(actions_module, 'subprocess', types.SimpleNamespace(
        run=refuse,
        Popen=refuse,
        SubprocessError=real_subprocess.SubprocessError,
        TimeoutExpired=real_subprocess.TimeoutExpired,
        CalledProcessError=real_subprocess.CalledProcessError))
    yield


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
