"""Process-free tests for action-layer state transitions.

These actions change applet state and persist picks; they start nothing, so
they are asserted here with a fake applet.  Anything that starts a locker,
blanker, editor, player or ``systemctl`` verb belongs to the recorded-fake
tests in ``tests/test_actions.py``, and real power-state verification stays a
manual step by design.
"""

from types import SimpleNamespace

import pytest

import pwr_tray.Actions as actions_module
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
