"""Direct tests for the extracted idle state machine.

`PwrTray` used to hold the whole branch ladder, so the only way to reach it was
to build a bare instance with ``object.__new__`` and hand-assign attributes.
The ladder now lives in :class:`~pwr_tray.IdleStateMachine.IdleStateMachine`,
which takes a small applet object, so these tests drive the real class with a
fake applet and never construct a tray, a Qt timer or a process.

No test here -- and no test in the suite -- may touch the machine's power
state; ``suspend``/``poweroff``/``lock``/``blank`` are recorded, not run.
"""

from types import SimpleNamespace

import pwr_tray.IdleStateMachine as idle_module
from pwr_tray.IdleStateMachine import IdleStateMachine


class FakeApplet:
    """The slice of PwrTray the state machine drives."""

    def __init__(self, mode='SleepAfterLock', running_idle_s=0.0,
                 state=('Awake', 0), power_down=False, turn_off_monitors=True,
                 was_inhibited='', idle_manager=None):
        self.mode = mode
        self.running_idle_s = running_idle_s
        self.state = SimpleNamespace(name=state[0], when=state[1])
        self.power_down = power_down
        self.turn_off_monitors = turn_off_monitors
        self.was_inhibited = was_inhibited
        self.idle_manager = idle_manager
        self.battery = SimpleNamespace(selector='Settings')
        self.quick = False
        self.variables = {'get_idle_ms': '', 'get_idle_s': ''}
        self.calls = []

    def update_running_idle_s(self):
        self.calls.append('read_idle')

    def show_icon(self, inhibited=''):
        self.calls.append(('icon', inhibited))

    def get_lock_min_list(self, selector=None):
        return [15, 30]

    def get_sleep_min_list(self, selector=None):
        return [5, 30]

    def get_effective_mode(self):
        return self.mode

    def get_params(self):
        return SimpleNamespace(power_down=self.power_down,
                               turn_off_monitors=self.turn_off_monitors)

    def reset_xidle_ms(self):
        self.calls.append('reset')

    def poweroff(self, _):
        self.calls.append('poweroff')

    def suspend(self, _):
        self.calls.append('suspend')

    def lock_screen(self, _):
        self.calls.append('lock')

    def blank_primitive(self):
        self.calls.append('blank')

    def set_state(self, name):
        self.calls.append(('state', name))


def _tick(**kwargs):
    applet = FakeApplet(**kwargs)
    IdleStateMachine(applet).tick()
    return applet


# -- the branch ladder ------------------------------------------------------

def test_power_down_prefers_poweroff():
    applet = _tick(running_idle_s=3600.0, power_down=True)

    assert applet.calls == ['read_idle', ('icon', ''), 'poweroff']


def test_without_power_down_the_machine_suspends():
    applet = _tick(running_idle_s=3600.0, power_down=False)

    assert applet.calls == ['read_idle', ('icon', ''), 'suspend']


def test_reaching_the_lock_timeout_locks():
    applet = _tick(running_idle_s=1000.0)

    assert applet.calls == ['read_idle', ('icon', ''), 'lock']


def test_a_locked_screen_blanks_after_the_blank_delay():
    applet = _tick(running_idle_s=1000.0, state=('Locked', 980))

    assert applet.calls == ['read_idle', ('icon', ''), 'blank']


def test_dropping_below_the_lock_timeout_returns_to_awake():
    applet = _tick(running_idle_s=10.0, state=('Locked', 0))

    assert applet.calls == ['read_idle', ('icon', ''), ('state', 'Awake')]


def test_presentation_resets_the_idle_clock():
    applet = _tick(running_idle_s=100.0, mode='Presentation')

    assert applet.calls == ['read_idle', ('icon', ''), 'reset']


def test_media_inhibit_resets_the_idle_clock():
    applet = _tick(running_idle_s=100.0, was_inhibited='player')

    assert applet.calls == ['read_idle', ('icon', 'player'), 'reset']


def test_a_running_idle_manager_is_asked_to_reset_directly():
    applet = _tick(running_idle_s=10.0, was_inhibited='systemd',
                   idle_manager=object())

    assert applet.calls == ['read_idle', ('icon', 'systemd'), 'reset']


def test_no_action_before_the_first_timeout():
    applet = _tick(running_idle_s=0.0)

    # Nothing is due yet, but the idle clock is still read and the icon shown.
    assert applet.calls == ['read_idle', ('icon', '')]


# -- state primitives -------------------------------------------------------

def test_set_state_records_the_idle_clock():
    applet = FakeApplet(running_idle_s=42.5)

    IdleStateMachine(applet).set_state('Locked')

    assert applet.state.name == 'Locked'
    assert applet.state.when == 42.5


def test_update_running_idle_s_reads_the_configured_command(monkeypatch):
    applet = FakeApplet()
    applet.variables['get_idle_ms'] = 'xprintidle'
    monkeypatch.setattr(idle_module.subprocess, 'check_output',
                        lambda argv: b'3000\n')

    IdleStateMachine(applet).update_running_idle_s()

    assert applet.running_idle_s == 3.0


def test_update_running_idle_s_logs_a_failing_command(monkeypatch):
    logged = []
    applet = FakeApplet()
    applet.running_idle_s = 7.0
    applet.variables['get_idle_ms'] = 'xprintidle'

    def fail(_argv):
        raise OSError('xprintidle is gone')

    monkeypatch.setattr(idle_module.subprocess, 'check_output', fail)
    monkeypatch.setattr(idle_module, 'prt',
                        lambda *args, **_kwargs: logged.append(args[0]))

    IdleStateMachine(applet).update_running_idle_s()

    assert applet.running_idle_s == 7.0
    assert any('idle time command failed' in line for line in logged)
