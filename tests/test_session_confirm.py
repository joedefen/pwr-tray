"""Log off / reboot / power off: armed in the menu, never on the idle path.

With ``confirm_session_end = True`` a session-ending entry becomes a two-step
click instead of a dialog: the label reads ``(click twice)``, the first click
arms it, the label reads ``(click NOW)``, and a second click within
``ARM_SECONDS`` runs it.  Only one entry can be armed at a time.

The other half of the rule is what must NOT be gated: ``IdleStateMachine``
calls ``PwrTray.poweroff()`` when a battery threshold is reached, and there is
nobody at the machine to click twice.
"""

import types
from pathlib import Path

import pwr_tray.Actions as actions_module
import pwr_tray.main as main_module
from pwr_tray.main import PwrTray


class _Completed:
    """The subset of CompletedProcess the action layer reads."""

    def __init__(self, returncode=0):
        self.returncode = returncode


def _tray(monkeypatch, confirm=True, **variables):
    """The slice of PwrTray the arming logic and the actions read."""
    tray = object.__new__(PwrTray)
    tray.variables = dict(variables)
    tray.get_params = lambda: types.SimpleNamespace(confirm_session_end=confirm)
    tray._armed = None
    tray._menu_arm = None
    tray._rendered_arm = None
    # The menu-open slot without Qt: these tests never render a real menu, so
    # building one is recorded nowhere and the bus is never asked.
    tray._bus_is_trusted = lambda: False
    tray._inhibit_rows = []
    tray.build_menu = lambda rows=None: None
    monkeypatch.setattr(PwrTray, 'singleton', tray)
    return tray


def test_the_setting_is_off_by_default():
    from pwr_tray.IniTool import IniTool

    assert IniTool.defaults['Settings']['confirm_session_end'] is False


def test_the_first_click_only_arms(monkeypatch):
    tray = _tray(monkeypatch)
    ran = []

    tray.arm_or_run('poweroff', 'Power off', ran.append)

    assert ran == []                      # nothing happened yet
    assert tray._armed_key() == 'poweroff'
    assert tray._confirm_suffix('poweroff') == ' (click NOW)'
    assert tray._confirm_suffix('reboot') == ' (click twice)'


def test_the_second_click_runs_it_and_disarms(monkeypatch):
    tray = _tray(monkeypatch)
    ran = []

    tray.arm_or_run('poweroff', 'Power off', ran.append)
    tray.arm_or_run('poweroff', 'Power off', ran.append)

    assert ran == [None]                  # the static wrappers take `checked`
    assert tray._armed_key() is None


def test_arming_another_entry_drops_the_pending_one(monkeypatch):
    tray = _tray(monkeypatch)
    ran = []

    tray.arm_or_run('poweroff', 'Power off', ran.append)
    tray.arm_or_run('reboot', 'Reboot', ran.append)

    assert tray._armed_key() == 'reboot'
    assert tray._confirm_suffix('poweroff') == ' (click twice)'
    assert ran == []


def test_the_arm_lapses_after_the_timeout(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(main_module.time, 'monotonic', lambda: clock[0])
    tray = _tray(monkeypatch)
    ran = []

    tray.arm_or_run('logoff', 'Log off', ran.append)
    assert tray._confirm_suffix('logoff') == ' (click NOW)'

    clock[0] += PwrTray.ARM_SECONDS + 0.01

    assert tray._armed_key() is None
    assert tray._confirm_suffix('logoff') == ' (click twice)'
    # A click after the deadline arms again; it never runs.
    tray.arm_or_run('logoff', 'Log off', ran.append)
    assert ran == []


def test_click_now_survives_the_deadline_while_the_menu_is_open(monkeypatch):
    """ARM_SECONDS governs reopening the menu, not reading it."""
    clock = [1000.0]
    monkeypatch.setattr(main_module.time, 'monotonic', lambda: clock[0])
    tray = _tray(monkeypatch)
    ran = []
    tray.arm_or_run('poweroff', 'Power off', ran.append)

    tray._on_menu_about_show()            # the menu opens: NOW is promised
    assert tray._menu_arm == 'poweroff'

    clock[0] += 600                       # ten minutes of reading
    assert tray._confirm_suffix('poweroff') == ' (click NOW)'
    tray.arm_or_run('poweroff', 'Power off', ran.append)
    assert ran == [None]


def test_closing_the_menu_ends_the_promise(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(main_module.time, 'monotonic', lambda: clock[0])
    tray = _tray(monkeypatch)
    tray.arm_or_run('poweroff', 'Power off', lambda _checked=None: None)
    tray._on_menu_about_show()

    clock[0] += PwrTray.ARM_SECONDS + 1
    tray._on_menu_about_hide()

    # Closed and lapsed: the next open must offer the double click again.
    assert tray._confirm_suffix('poweroff') == ' (click twice)'


def test_opening_the_menu_rebuilds_when_the_arm_changes(monkeypatch):
    """The bug this guards: aboutToShow rebuilt only for row/label changes."""
    tray = _tray(monkeypatch)
    tray._bus_is_trusted = lambda: False
    tray._inhibit_rows = []
    built = []
    tray.build_menu = lambda rows=None: built.append(rows)
    tray.arm_or_run('poweroff', 'Power off', lambda _checked=None: None)

    tray._on_menu_about_show()
    assert tray._menu_arm == 'poweroff'
    assert len(built) == 1                # so the NOW label gets rendered

    tray._rendered_arm = 'poweroff'       # what the real build_menu records
    tray._on_menu_about_show()
    assert len(built) == 1                # nothing changed: no rebuild


def test_the_desktop_list_decides_which_keys_are_armed(monkeypatch):
    tray = _tray(monkeypatch, session_end=['logoff', 'restart_wm'])

    assert tray.session_end_keys() == ('logoff', 'restart_wm')


def test_an_old_my_commands_yaml_falls_back_to_the_builtin_list(monkeypatch):
    """The key is new: a user file that predates it must still be confirmed."""
    tray = _tray(monkeypatch)

    assert tray.session_end_keys() == main_module.DEFAULT_SESSION_END


def test_the_idle_path_never_touches_the_arm_state(monkeypatch):
    """IdleStateMachine reaches the action with nobody there to click."""
    started = []
    monkeypatch.setattr(actions_module, 'subprocess', types.SimpleNamespace(
        run=lambda command, **kwargs: started.append(command) or _Completed(),
        Popen=lambda command, **kwargs: started.append(command) or _Completed()))
    tray = _tray(monkeypatch, poweroff='systemctl poweroff')

    def explode():
        raise AssertionError('the automatic path must not consult the arm state')

    monkeypatch.setattr(tray, '_armed_key', explode)
    PwrTray.poweroff(None)

    assert started == ['systemctl poweroff']


def test_the_confirmation_is_a_label_and_not_a_dialog():
    """A parentless toplevel is at the compositor's mercy; a label is not."""
    source = Path(main_module.__file__).read_text(encoding='utf-8')

    assert 'QMessageBox.question' not in source
    assert 'def session_end(' not in source
    assert 'arm_or_run(key, what, action)' in source
