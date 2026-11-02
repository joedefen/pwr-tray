"""Regression tests for the action layer.

Policy was tested; the commands the policy triggers were not.  Every menu entry
ends in a ``subprocess`` call whose *exact text* is desktop knowledge:

* ``swaymsg "output * dpms off"`` must reach the shell with its quotes intact,
  or `swaymsg` receives the words ``output``, ``*``, ``dpms``, ``off``;
* ``sleep 1.0; exec xset dpms force off`` is only meaningful as a shell line;
* ``~/.config/pwr-tray/lockpaper.png`` is expanded by the shell, so passing
  the locker as an argv list would hand `swaylock` a literal ``~``;
* KDE/XFCE/Cinnamon lock themselves on suspend, so the applet must *not* start
  a locker first.

That knowledge lives in ``pwr_tray/resources/commands.yaml`` plus a handful of
methods, and used to be verified only by running the applet on the real
desktop.  These tests read the YAML the applet ships, build the variables with
the same :meth:`~pwr_tray.main.PwrTray.build_variables` that ``__init__`` uses,
and assert the exact call for every supported desktop -- so changing an
argument, or dropping a shell, fails a test.
"""

import subprocess as real_subprocess
import types

import pytest

import pwr_tray.Actions as actions_module
from pwr_tray.main import PwrTray
from pwr_tray.Utils import resource_text

WALLPAPER = '~/.config/pwr-tray/lockpaper.png'
SWAYLOCK = (f'swaylock -i {WALLPAPER} --ignore-empty-password'
            ' --show-failed-attempts')

# The command each desktop declares, exactly as the applet would hand it to a
# shell.  An empty value means the desktop leaves that action unbound.
LOCKERS = {
    'i3-x11': (f'i3lock -t -i {WALLPAPER} --ignore-empty-password'
               ' --show-failed-attempts'),
    'sway-wayland': SWAYLOCK,
    'hyprland-wayland': SWAYLOCK,
    'niri-wayland': SWAYLOCK,
    'kde-x11': 'loginctl lock-session',
    'kde-wayland': 'loginctl lock-session',
    'xfce-x11': 'loginctl lock-session',
    'cinnamon-x11': 'loginctl lock-session',
    'mate-x11': 'loginctl lock-session',
    'lxqt-x11': 'loginctl lock-session',
}
ALL_DESKTOPS = sorted(LOCKERS)

MONITORS_OFF = {
    'i3-x11': 'sleep 1.0; exec xset dpms force off',
    'sway-wayland': 'swaymsg "output * dpms off"',
    'hyprland-wayland': 'hyprctl dispatch dpms off',
    'niri-wayland': 'niri msg action power-off-monitors',
}

# (desktop, method, command); an empty command must start nothing at all.
WM_ACTIONS = [
    ('i3-x11', 'reload_wm', 'i3-msg reload'),
    ('i3-x11', 'restart_wm', 'i3-msg restart'),
    ('i3-x11', 'exit_wm', 'i3-msg exit'),
    ('sway-wayland', 'reload_wm', 'swaymsg reload'),
    ('sway-wayland', 'restart_wm', ''),
    ('sway-wayland', 'exit_wm', 'swaymsg exit'),
    ('hyprland-wayland', 'reload_wm', 'hyprctl reload'),
    ('hyprland-wayland', 'restart_wm', ''),
    ('hyprland-wayland', 'exit_wm', 'hyprctl dispatch exit'),
    ('niri-wayland', 'reload_wm', 'niri msg action reload-config'),
    ('niri-wayland', 'restart_wm', ''),
    ('niri-wayland', 'exit_wm', 'niri msg action quit --skip-confirmation'),
    ('kde-x11', 'reload_wm', ''),
    ('kde-x11', 'restart_wm',
     'killall plasmashell && kstart5 plasmashell && sleep 3 && pwr-tray'),
    ('kde-x11', 'exit_wm',
     'qdbus org.kde.Shutdown /Shutdown org.kde.Shutdown.logout'),
    ('kde-wayland', 'reload_wm', ''),
    ('kde-wayland', 'restart_wm', ''),
    ('kde-wayland', 'exit_wm', 'pkill kwin'),
    ('xfce-x11', 'reload_wm', 'xfce4-panel -r'),
    ('xfce-x11', 'exit_wm', 'xfce4-session-logout --logout'),
    ('cinnamon-x11', 'exit_wm', 'cinnamon-session-quit --logout'),
    ('mate-x11', 'exit_wm', 'mate-session-save --logout'),
    ('lxqt-x11', 'exit_wm', 'lxqt-leave --logout'),
]


# -- harness ---------------------------------------------------------------

class _Completed:
    """The subset of CompletedProcess the action layer reads."""

    def __init__(self, returncode=0):
        self.returncode = returncode


class _Recorder:
    """Records every process the action layer starts.

    Each call is ``(kind, command, needs_a_shell)``.  `command` is the string
    handed to ``/bin/sh`` for shell calls, and the argv list for the two calls
    that pass one (`edit_config`, `restart_self`).
    """

    def __init__(self):
        self.calls = []

    def run(self, command, **kwargs):
        self.calls.append(('run', command, bool(kwargs.get('shell'))))
        return _Completed()

    def Popen(self, command, **kwargs):
        self.calls.append(('Popen', command, bool(kwargs.get('shell'))))
        return _Completed()


@pytest.fixture(name='rec', autouse=True)
def recorder_fixture(monkeypatch):
    """Record every process the action layer would start.

    ``autouse=True`` is deliberate: the action layer owns real ``subprocess``
    calls, so a test that forgets to request this fixture must still be unable
    to lock the session, blank the monitors or power the machine off.  The
    recorder is installed on the module that performs the calls, not on
    ``main``.
    """
    recorder = _Recorder()
    monkeypatch.setattr(actions_module, 'subprocess', types.SimpleNamespace(
        run=recorder.run,
        Popen=recorder.Popen,
        DEVNULL=real_subprocess.DEVNULL,
        SubprocessError=real_subprocess.SubprocessError,
        TimeoutExpired=real_subprocess.TimeoutExpired,
        CalledProcessError=real_subprocess.CalledProcessError))
    monkeypatch.setattr(actions_module.time, 'sleep', lambda _seconds: None)
    return recorder


def _scalar(value):
    """Read one YAML scalar the way commands.yaml writes them."""
    if value.startswith('[') and value.endswith(']'):
        return [item.strip() for item in value[1:-1].split(',') if item.strip()]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in '\'"':
        return value[1:-1]
    return value


def _parse_commands(text):
    """Parse the subset of YAML that commands.yaml uses, without ruamel.yaml.

    The suite has to run in a bare container where the YAML library is only a
    stub (see conftest.py), and this file is a two-level map of scalars, so a
    short reader is more honest than skipping the F4 tests exactly where they
    matter most.  `test_the_reader_matches_the_shipped_file` fails loudly if
    the file ever grows a shape this does not understand.
    """
    data = {}
    section = entry = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        key, _, value = line.partition(':')
        indent = len(raw) - len(raw.lstrip())
        key, value = key.strip(), value.strip()
        if indent == 0:
            section, entry = data.setdefault(key, {}), None
        elif not value:
            entry = section.setdefault(key, {})
        elif entry is None:
            section[key] = _scalar(value)
        else:
            entry[key] = _scalar(value)
    return data


def _desktop(desktop):
    """The variables and merged config a real launch of `desktop` would get."""
    de_json = _parse_commands(resource_text('commands.yaml'))
    de_config = PwrTray.detect_de(de_json, force_de=desktop,
                                  log=lambda *_args: None)
    return PwrTray.build_variables(de_json, de_config), de_config


def test_the_reader_matches_the_shipped_file():
    """Anchor the reader above: ten desktops, and the values we rely on."""
    data = _parse_commands(resource_text('commands.yaml'))

    assert sorted(data['desktops']) == ALL_DESKTOPS
    assert data['defaults']['suspend'] == 'systemctl suspend'
    assert data['session_types']['x11']['idle_method'] == 'poll'
    assert data['desktops']['sway-wayland']['monitors_off'] == (
        'swaymsg "output * dpms off"')


@pytest.fixture(name='tray')
def tray_fixture(monkeypatch):
    """Build a bare PwrTray for one desktop, with the state side effects hidden.

    set_state/reset_xidle_ms/update_running_idle_s are recorded rather than
    run: this file is about which commands are started, and in which order.
    """
    def make(desktop, **params):
        variables, de_config = _desktop(desktop)
        tray = object.__new__(PwrTray)
        tray.variables = variables
        tray.de_config = de_config
        tray.is_wayland = de_config['session_type'] == 'wayland'
        tray.states = []
        tray.set_state = tray.states.append
        tray.reset_xidle_ms = lambda: tray.states.append('reset_idle')
        tray.update_running_idle_s = lambda: tray.states.append('read_idle')
        tray.get_params = lambda: types.SimpleNamespace(
            turn_off_monitors=params.get('turn_off_monitors', True),
            gui_editor=params.get('gui_editor', 'geany'),
            log_viewer=params.get('log_viewer', 'xterm -e'))
        tray.ini_tool = types.SimpleNamespace(
            ini_path='/tmp/pwr-tray/config.ini',
            log_path='/tmp/pwr-tray/debug.log',
            log_old_path='/tmp/pwr-tray/debug.log.old')
        monkeypatch.setattr(PwrTray, 'singleton', tray)
        return tray
    return make


def _shell(command):
    """The recorded call a desktop command should produce."""
    return ('run', command, True)


# -- locking ---------------------------------------------------------------

@pytest.mark.parametrize('desktop', ALL_DESKTOPS)
def test_lock_screen_runs_this_desktops_locker(tray, rec, desktop):
    tray = tray(desktop)
    PwrTray.lock_screen(None)

    assert rec.calls == [_shell(LOCKERS[desktop])]
    # The idle clock is re-read after locking, and the state says "Locked".
    assert tray.states == ['read_idle', 'Locked']


# -- blanking --------------------------------------------------------------

@pytest.mark.parametrize('desktop', sorted(MONITORS_OFF))
def test_blanking_uses_this_desktops_monitor_command(tray, rec, desktop):
    tray = tray(desktop)
    tray.blank_primitive(lock_screen=False)

    assert rec.calls == [_shell(MONITORS_OFF[desktop])]
    assert tray.states == ['Blanked']


def test_blank_quick_locks_before_blanking(tray, rec):
    """Order matters: blanking a screen that is not yet locked leaks it."""
    tray = tray('sway-wayland')
    PwrTray.blank_quick(None)

    assert rec.calls == [
        _shell(LOCKERS['sway-wayland']),
        _shell(MONITORS_OFF['sway-wayland']),
    ]
    assert tray.states == ['read_idle', 'Locked', 'Blanked']


def test_blanking_without_a_monitor_command_does_nothing(tray, rec):
    """kde-wayland has no monitors_off: say so, start nothing."""
    tray = tray('kde-wayland')
    tray.blank_primitive(lock_screen=False)

    assert rec.calls == []
    assert tray.states == []


def test_blanking_obeys_the_turn_off_monitors_setting(tray, rec):
    tray = tray('sway-wayland', turn_off_monitors=False)
    tray.blank_primitive(lock_screen=False)

    assert rec.calls == []
    assert tray.states == []


# -- suspend, poweroff, reboot ---------------------------------------------

def test_suspend_locks_first_on_a_tiling_desktop(tray, rec):
    """niri/sway/i3 do not lock on suspend themselves, so the applet must."""
    tray = tray('sway-wayland')
    PwrTray.suspend(None)

    assert rec.calls == [
        ('Popen', LOCKERS['sway-wayland'], True),   # async: must not block
        _shell('systemctl suspend'),
    ]
    assert tray.states == ['Asleep', 'reset_idle']


@pytest.mark.parametrize('desktop', ['kde-wayland', 'kde-x11', 'xfce-x11',
                                     'cinnamon-x11'])
def test_a_full_de_locks_itself_so_suspend_does_not(tray, rec, desktop):
    tray = tray(desktop)
    PwrTray.suspend(None)

    assert rec.calls == [_shell('systemctl suspend')]


@pytest.mark.parametrize('desktop', ALL_DESKTOPS)
@pytest.mark.parametrize('action,verb', [
    (PwrTray.poweroff, 'poweroff'),
    (PwrTray.reboot, 'reboot'),
])
def test_poweroff_and_reboot_go_through_systemctl(tray, rec, desktop,
                                                  action, verb):
    tray(desktop)
    action(None)

    assert rec.calls == [_shell(f'systemctl {verb}')]


# -- window-manager actions ------------------------------------------------

@pytest.mark.parametrize('desktop,method,command', WM_ACTIONS)
def test_wm_actions_match_the_declared_command(tray, rec, desktop, method,
                                               command):
    tray = tray(desktop)
    getattr(PwrTray, method)(None)

    assert rec.calls == ([_shell(command)] if command else [])


# -- run_command() itself --------------------------------------------------

def test_run_command_hands_the_line_to_the_shell_untouched(tray, rec):
    """`swaymsg "output * dpms off"` is one line; splitting it breaks sway."""
    tray = tray('sway-wayland')
    tray.variables['probe'] = 'swaymsg "output * dpms off"'
    PwrTray.run_command('probe')

    assert rec.calls == [_shell('swaymsg "output * dpms off"')]


def test_run_command_keeps_sequences_whole(tray, rec):
    tray = tray('i3-x11')
    tray.variables['probe'] = 'sleep 1.0; exec xset dpms force off'
    PwrTray.run_command('probe')

    assert rec.calls == [_shell('sleep 1.0; exec xset dpms force off')]


def test_run_command_ignores_a_key_the_desktop_leaves_empty(tray, rec):
    tray = tray('sway-wayland')
    PwrTray.run_command('restart_wm')

    assert rec.calls == []


def test_run_command_reports_a_nonzero_status(tray, rec, monkeypatch):
    logged = []
    monkeypatch.setattr(actions_module, 'prt',
                        lambda *args, **_kwargs: logged.append(' '.join(
                            str(arg) for arg in args)))
    tray = tray('sway-wayland')
    tray.variables['probe'] = 'swaymsg reload'
    monkeypatch.setattr(actions_module.subprocess, 'run',
                        lambda *_a, **_k: _Completed(returncode=3))

    PwrTray.run_command('probe')

    assert '+ swaymsg reload' in logged
    assert any('NOTE: returncode=3' in line for line in logged)


# -- misc actions ----------------------------------------------------------

@pytest.mark.parametrize('desktop,expected', [
    ('i3-x11', _shell('xset s reset')),
    ('sway-wayland', None),          # swayidle owns the idle clock
])
def test_reset_idle_only_exists_on_x11(tray, rec, desktop, expected):
    tray = tray(desktop)
    PwrTray.reset_xidle_ms(tray)

    assert rec.calls == ([expected] if expected else [])


def test_edit_config_runs_the_configured_editor_as_argv(tray, rec):
    """Not a shell line: gui_editor is a word list plus one quoted path."""
    tray('sway-wayland')
    PwrTray.edit_config(None)

    assert rec.calls == [('run', ['geany', '/tmp/pwr-tray/config.ini'], False)]


def test_edit_config_keeps_the_editor_out_of_our_log(tray, monkeypatch):
    """The editor must not inherit debug.log as its stdout.

    gui_editor is often a terminal emulator (konsole -e vim), and prt() has
    pointed our fds 1/2 at debug.log: without redirection the editor appends
    its own toolkit's raw output to the log, where no message handler of ours
    can see it.
    """
    seen = []
    tray('sway-wayland')
    monkeypatch.setattr(actions_module.subprocess, 'run',
                        lambda argv, **kwargs: seen.append((argv, kwargs)))

    PwrTray.edit_config(None)

    assert seen == [(['geany', '/tmp/pwr-tray/config.ini'],
                     {'check': True,
                      'stdin': real_subprocess.DEVNULL,
                      'stdout': real_subprocess.DEVNULL,
                      'stderr': real_subprocess.DEVNULL})]


def test_edit_config_does_nothing_without_a_gui_editor(tray, rec):
    tray('sway-wayland', gui_editor='')
    PwrTray.edit_config(None)

    assert rec.calls == []
