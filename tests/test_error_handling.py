"""Regression tests for startup-failure reporting.

`detect_de()` and the must_haves check used to abort with `assert`, which shows
the user a traceback -- and, worse, disappears under `python -O`, leaving the
application running with empty command strings (silent no-ops).  These tests
pin down the replacement: `PwrTrayError` subclasses that `main()` prints
without a traceback, plus a `--list-des` report that doubles as the
missing-command check the README promises.
"""

import os
import subprocess
import sys
import types

import pytest

from pwr_tray.Errors import DesktopDetectionError, MissingCommandsError, PwrTrayError
from pwr_tray.main import PwrTray, format_desktops

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DE_JSON = {
    'defaults': {'suspend': 'systemctl suspend',
                 'must_haves': ['systemctl']},
    'session_types': {
        'x11': {'must_haves': ['xprintidle', 'xset']},
        'wayland': {'must_haves': ['swayidle']},
    },
    'desktops': {
        'i3-x11': {'locker': 'i3lock', 'must_haves': ['i3lock']},
        'sway-wayland': {'locker': 'swaylock', 'must_haves': ['swaylock']},
    },
}


@pytest.fixture
def session(monkeypatch):
    """Set the desktop-detection environment for one test."""
    def apply(current='', session_desktop='', desktop_session='',
              session_type=''):
        monkeypatch.setenv('XDG_CURRENT_DESKTOP', current)
        monkeypatch.setenv('XDG_SESSION_DESKTOP', session_desktop)
        monkeypatch.setenv('DESKTOP_SESSION', desktop_session)
        monkeypatch.setenv('XDG_SESSION_TYPE', session_type)
    return apply


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

def test_known_desktop_is_still_detected(session):
    session(current='sway', session_type='wayland')
    config = PwrTray.detect_de(DE_JSON)
    assert config['name'] == 'sway-wayland'
    assert config['session_type'] == 'wayland'
    assert config['locker'] == 'swaylock'
    assert config['must_haves'] == ['swayidle', 'swaylock', 'systemctl']


def test_unknown_session_raises_actionable_error(session):
    session(current='GNOME', session_type='wayland')
    with pytest.raises(DesktopDetectionError) as raised:
        PwrTray.detect_de(DE_JSON)

    message = str(raised.value)
    assert 'no desktop environment matched' in message
    assert "XDG_CURRENT_DESKTOP='GNOME'" in message
    # The known-DE list is what makes the message actionable.
    assert 'i3-x11' in message and 'sway-wayland' in message
    assert 'pwr-tray --de NAME' in message
    assert 'pwr-tray --list-des' in message


def test_unknown_forced_desktop_raises_actionable_error(session):
    session(current='sway', session_type='wayland')
    with pytest.raises(DesktopDetectionError) as raised:
        PwrTray.detect_de(DE_JSON, force_de='gnome-wayland')

    message = str(raised.value)
    assert "'gnome-wayland'" in message
    assert 'sway-wayland' in message


def test_detection_guard_survives_python_O():
    """`python -O` strips asserts; the guard must still fire.

    The subprocess is self-contained (it installs its own dependency stubs)
    because the autouse HOME isolation hides the user-site pytest install.
    """
    script = f"""
import os
import sys
import types


class _Stub:
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


def _module(name, **attrs):
    module = types.ModuleType(name)
    module.__file__ = '<stub ' + name + '>'

    def _module_getattr(attr):
        if attr.startswith('__'):
            raise AttributeError(attr)
        return _Stub()

    module.__getattr__ = _module_getattr
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    parent, _, leaf = name.rpartition('.')
    if parent:
        setattr(sys.modules[parent], leaf, module)


for _parent in ('PyQt5', 'ruamel'):
    sys.modules[_parent] = types.ModuleType(_parent)
_module('PyQt5.QtCore', QTimer=_Stub)
_module('PyQt5.QtGui', QIcon=_Stub, QCursor=_Stub)
_module('PyQt5.QtWidgets', QApplication=_Stub, QSystemTrayIcon=_Stub,
        QMenu=_Stub, QAction=_Stub)
_module('ruamel.yaml', YAML=_Stub)
_module('psutil', sensors_battery=lambda: None)

sys.path.insert(0, {ROOT!r})

os.environ.update(XDG_CURRENT_DESKTOP='gnome',
                  XDG_SESSION_DESKTOP='gnome',
                  DESKTOP_SESSION='gnome',
                  XDG_SESSION_TYPE='wayland')

from pwr_tray.Errors import DesktopDetectionError
from pwr_tray.main import PwrTray

DE_JSON = {{'defaults': {{'must_haves': []}}, 'session_types': {{}},
            'desktops': {{'i3-x11': {{}}}}}}
try:
    PwrTray.detect_de(DE_JSON)
except DesktopDetectionError:
    print('REPORTED')
else:
    print('SILENT')
    sys.exit(1)
"""
    result = subprocess.run([sys.executable, '-O', '-c', script],
                            capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stderr
    assert 'REPORTED' in result.stdout
    assert 'Traceback' not in result.stderr


# --------------------------------------------------------------------------
# must_haves
# --------------------------------------------------------------------------

def test_must_haves_merge_all_three_layers():
    assert PwrTray.must_haves_for(DE_JSON, 'i3-x11') == [
        'i3lock', 'systemctl', 'xprintidle', 'xset']


def test_missing_commands_reports_only_absent_binaries(monkeypatch):
    # missing_commands() now lives in Desktop.py, so that is where $PATH is
    # intercepted; PwrTray.missing_commands is a thin alias to it.
    monkeypatch.setattr(
        'pwr_tray.Desktop.shutil.which',
        lambda cmd: None if cmd == 'gone' else f'/usr/bin/{cmd}')
    assert PwrTray.missing_commands({'must_haves': ['sh', 'gone']}) == ['gone']


def test_missing_command_error_says_what_to_install():
    message = str(MissingCommandsError(['xprintidle'], desktop='i3-x11',
                                       session_type='x11'))
    assert 'xprintidle' in message
    assert 'i3-x11 (x11)' in message
    assert 'package manager' in message
    assert '--list-des' in message


def test_user_facing_errors_share_a_base_class():
    assert issubclass(DesktopDetectionError, PwrTrayError)
    assert issubclass(MissingCommandsError, PwrTrayError)


# --------------------------------------------------------------------------
# --list-des
# --------------------------------------------------------------------------

def test_list_des_report_marks_missing_commands(monkeypatch):
    monkeypatch.setattr(
        'pwr_tray.main.shutil.which',
        lambda cmd: '/usr/bin/systemctl' if cmd == 'systemctl' else None)

    report = '\n'.join(format_desktops(DE_JSON, 'test commands.yaml'))

    assert 'from test commands.yaml' in report
    assert 'i3-x11' in report and 'sway-wayland' in report
    assert 'i3lock [!]' in report
    assert 'swaylock [!]' in report
    # systemctl is on $PATH in this scenario, so it is not marked.
    assert 'systemctl' in report
    assert 'systemctl [!]' not in report
    assert 'not found on $PATH' in report


# --------------------------------------------------------------------------
# main() integration
# --------------------------------------------------------------------------

def test_main_reports_pwr_tray_error_without_traceback(monkeypatch):
    """main() must print the message and exit 2, not raise a traceback."""
    import pwr_tray.main as main_module

    class FakeIniTool:
        """Stand-in that never touches the user's real config."""
        folder = '/tmp/pwr-tray-test'
        ini_path = os.path.join(folder, 'config.ini')
        log_path = os.path.join(folder, 'debug.log')
        picks_path = os.path.join(folder, 'picks.json')

        def __init__(self, paths_only=False):
            pass

        def update_config(self):
            return False

        def get_selectors(self):
            return []

    class ExplodingTray:
        """Startup that fails the way an unsupported session does."""
        @staticmethod
        def goodbye(message=''):
            pass

        def __init__(self, *args, **kwargs):
            raise MissingCommandsError(['xprintidle'], desktop='i3-x11',
                                       session_type='x11')

    class FakeLock:
        """Stands in for Utils.SingleInstance (real one would touch the FS)."""
        def __init__(self, path):
            self.path = path

        def acquire(self, **kwargs):
            return True

        def release(self):
            pass

        @staticmethod
        def read_pid(path):
            return None

    reported = []
    fake_utils = types.SimpleNamespace(prt_path='',
                                       capture_console=lambda: None,
                                       error_print=reported.append,
                                       SingleInstance=FakeLock)

    monkeypatch.setattr(main_module, 'IniTool', FakeIniTool)
    monkeypatch.setattr(main_module, 'PwrTray', ExplodingTray)
    monkeypatch.setattr(main_module, 'Utils', fake_utils)
    monkeypatch.setattr(main_module, 'prt', lambda *args, **kw: None)
    monkeypatch.setattr(main_module, 'PyKill',
                        lambda *args, **kw: types.SimpleNamespace(
                            kill_pid=lambda *a, **k: False))
    monkeypatch.setattr(main_module, 'wait_for_tray', lambda *a, **k: True)
    monkeypatch.setattr(sys, 'argv', ['pwr-tray'])

    with pytest.raises(SystemExit) as raised:
        main_module.main()

    assert raised.value.code == 2
    assert len(reported) == 1
    assert 'xprintidle' in reported[0]
    assert 'Traceback' not in reported[0]
