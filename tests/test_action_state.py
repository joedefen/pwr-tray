"""Process-free tests for action-layer state transitions.

These actions change applet state and persist picks; they start nothing, so
they are asserted here with a fake applet.  Anything that starts a locker,
blanker, editor, player or ``systemctl`` verb belongs to the recorded-fake
tests in ``tests/test_actions.py``, and real power-state verification stays a
manual step by design.

The one exception is the unit-aware Quit/Restart pair: the command they hand
to systemd is the whole point (it is what distinguishes a restart from a
stop), so it is asserted here with ``Utils.spawn_detached`` faked.
"""

from types import SimpleNamespace

import pytest

import pwr_tray.Actions as actions_module
from pwr_tray import service
from pwr_tray.Actions import Actions


class FakeApplet:
    def __init__(self):
        self.mode = 'SleepAfterLock'
        self.was_output = ''
        self.saves = 0
        self.idle_starts = []
        self.hidden = 0
        self.tray_icon = SimpleNamespace(hide=self._hide)

    def _hide(self):
        self.hidden += 1

    def save_picks(self):
        self.saves += 1

    def idle_manager_start(self):
        self.idle_starts.append('idle')


@pytest.mark.parametrize('method,mode', [
    ('enable_presentation_mode', 'Presentation'),
    ('enable_nosleep_mode', 'LockOnly'),
    ('enable_normal_mode', 'SleepAfterLock'),
])
def test_mode_switches_persist_and_restart_the_idle_manager(monkeypatch, method,
                                                            mode):
    monkeypatch.setattr(actions_module, 'prt', lambda *args, **kwargs: None)
    applet = FakeApplet()

    getattr(Actions(applet), method)()

    assert applet.mode == mode
    assert applet.was_output == 'Changed Mode'
    assert applet.saves == 1
    assert applet.idle_starts == ['idle']


def test_quit_self_hides_the_icon_and_exits(monkeypatch):
    monkeypatch.setattr(actions_module, 'prt', lambda *args, **kwargs: None)
    applet = FakeApplet()

    with pytest.raises(SystemExit):
        Actions(applet).quit_self()

    assert applet.hidden == 1


class FakeApp:
    """The one method the unit-aware actions call on the application."""

    def __init__(self):
        self.quits = 0

    def quit(self):
        self.quits += 1


def _systemd_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(actions_module, 'prt', lambda *args, **kwargs: None)
    monkeypatch.setattr(
        actions_module.subprocess, 'run',
        lambda argv, **kwargs: calls.append((argv, kwargs)))
    return calls


def test_quit_self_under_systemd_stops_the_unit(monkeypatch):
    """A clean exit is not a stop for a unit with Restart=always.

    Under the unit, Quit must tell systemd to stop it; the applet then leaves
    through the normal Qt exit so atexit can release the lock and write ENDED.
    """
    calls = _systemd_calls(monkeypatch)
    applet = FakeApplet()
    applet.svc_mode = True
    applet.app = FakeApp()

    Actions(applet).quit_self()

    assert [call[0] for call in calls] == [
        service.systemctl('--no-block', 'stop', service.UNIT_NAME)]
    # --no-block: the job is queued before systemctl waits for the unit.
    assert calls[0][1]['timeout'] == actions_module.SYSTEMCTL_TIMEOUT
    assert applet.hidden == 1
    assert applet.app.quits == 1


def test_restart_self_under_systemd_restarts_the_unit(monkeypatch):
    """The replacement must not be spawned into the cgroup systemd kills.

    That spawn is why the menu's Restart used to look like 'stop for the
    session'.  Under the unit it must ask systemd to run the unit's own
    ExecStart again instead.
    """
    calls = _systemd_calls(monkeypatch)
    applet = FakeApplet()
    applet.svc_mode = True
    applet.app = FakeApp()

    Actions(applet).restart_self()

    assert [call[0] for call in calls] == [
        service.systemctl('--no-block', 'restart', service.UNIT_NAME)]
    assert calls[0][1]['timeout'] == actions_module.SYSTEMCTL_TIMEOUT
    assert applet.hidden == 1
    assert applet.saves == 1
    assert applet.app.quits == 1


def test_restart_self_under_systemd_falls_back_to_a_failure_exit(monkeypatch):
    """No way to start systemctl: exit non-zero so Restart=on-failure acts."""
    _systemd_calls(monkeypatch)

    def refuse(*_args, **_kwargs):
        raise OSError('systemctl is not on PATH')

    monkeypatch.setattr(actions_module.subprocess, 'run', refuse)
    applet = FakeApplet()
    applet.svc_mode = True
    applet.app = FakeApp()

    with pytest.raises(SystemExit) as raised:
        Actions(applet).restart_self()

    assert raised.value.code == 1
    assert applet.app.quits == 0
