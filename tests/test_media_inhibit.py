"""Regression tests for the media-inhibit path.

v1 read ``playerctl status`` and then never used it, so "stop the autolock while
a video plays" was a no-op on every desktop.  The fix has two halves and the
suite used to cover neither:

* :meth:`PwrTray.check_inhibited` turns ``playing`` into ``inhibited='player'``
  -- but only when no real logind inhibitor is present, because a logind
  inhibitor already owns the StopSign icon; and
* :meth:`SwayIdleManager.build_cmd` is where that decision is *consumed* on
  Wayland: while media plays, no idle timeouts are handed to swayidle.

The tests below drive both with a fake ``subprocess`` and a bare ``PwrTray``,
so they assert the decision rather than the presence of a player on the
machine.  Both would fail if the two code paths were reverted.
"""

from types import SimpleNamespace

import pytest

import pwr_tray.SwayIdleMgr as sway_module
from pwr_tray.main import PwrTray
from pwr_tray.SwayIdleMgr import SwayIdleManager

# -- fake subprocess --------------------------------------------------------

# What `busctl get-property ... BlockInhibited` prints: one line, one string.
# '' (nothing blocked) and None (could not read) are different answers.
NOTHING_BLOCKED = 's ""\n'
SLEEP_BLOCKED = 's "sleep"\n'
IDLE_BLOCKED = 's "idle"\n'
SLEEP_AND_IDLE = 's "sleep:idle"\n'
POWER_KEY_ONLY = 's "handle-power-key"\n'
SHUTDOWN_BLOCKED = 's "shutdown"\n'
LID_SWITCH_BLOCKED = 's "handle-lid-switch:shutdown"\n'

# `systemd-inhibit --no-pager --no-legend --mode=block`: rows only, no header
# and no "N inhibitors listed." footer.  The niri row is this project's real
# output on the machine it was developed on: a compositor owning the power key,
# which is an input event and not a request to stay awake.
INHIBITOR_TABLE = (
    'joe 1000 joe 4242 firefox sleep video block\n'
    'niri 1000 joe 4951 niri handle-power-key Power key handling block\n'
)


class _Completed:
    """The subset of CompletedProcess that check_inhibited() reads."""

    def __init__(self, stdout='', returncode=0, stderr=''):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _run_stub(commands, blocked, play_state='', table='', failures=None):
    """A subprocess.run that answers the queries check_inhibited() makes.

    `failures` maps an executable name to ``(returncode, stderr)`` so the
    failure paths are driven without a broken machine.
    """
    def run(argv, **_kwargs):
        commands.append(list(argv))
        returncode, stderr = (failures or {}).get(argv[0], (0, ''))
        if argv[0] == 'busctl':
            return _Completed(blocked, returncode, stderr)
        if argv[0] == 'systemd-inhibit':
            return _Completed(table, returncode, stderr)
        if argv[0] == 'playerctl':
            return _Completed(f'{play_state}\n', returncode, stderr)
        raise AssertionError(f'unexpected command {argv!r}')
    return run


# -- bare PwrTray -----------------------------------------------------------

class _FakeIcon:
    def __init__(self, index):
        self.index = index


class _FakeTrayIcon:
    """Records which icon show_icon() selected."""

    def __init__(self):
        self.shown = []

    def setIcon(self, icon):
        self.shown.append(icon.index)


def _tray(enable_playerctl=True, has_playerctl=True):
    """A PwrTray stripped down to what check_inhibited() touches."""
    tray = object.__new__(PwrTray)
    tray.has_playerctl = has_playerctl
    tray.enable_playerctl = enable_playerctl
    tray.mode = 'SleepAfterLock'
    tray.battery = SimpleNamespace(selector='Settings', percent=100,
                                   plugged=True, present=True)
    tray.was_play_state = ''
    tray.was_inhibited = None
    tray.was_output = ''
    tray._inhibit_warning = ''
    tray.poll_100ms = False
    tray.running_idle_s = 0.0
    tray.current_icon_num = -1
    tray.icons = [_FakeIcon(index) for index in range(8)]
    tray.tray_icon = _FakeTrayIcon()
    tray.get_lock_min_list = lambda selector=None: [15, 30]
    tray.DB = lambda: False
    tray.idle_starts = []
    tray.idle_manager_start = lambda: tray.idle_starts.append('idle')
    return tray


def _check(monkeypatch, blocked, play_state='', table='', failures=None,
           **tray_kwargs):
    tray = _tray(**tray_kwargs)
    commands = []
    monkeypatch.setattr(
        'pwr_tray.main.subprocess.run',
        _run_stub(commands, blocked, play_state, table, failures))
    rows, updated = tray.check_inhibited()
    return SimpleNamespace(tray=tray, rows=rows, updated=updated,
                           commands=commands)


# -- check_inhibited(): playerctl -> inhibited='player' ---------------------

def test_playing_media_sets_the_player_inhibit(monkeypatch):
    """v1 #1: the play state must reach was_inhibited, not just be read."""
    result = _check(monkeypatch, NOTHING_BLOCKED, 'Playing')

    assert result.tray.was_inhibited == 'player'
    assert result.rows == []
    assert result.updated is True            # the menu shows the new rows
    assert result.tray.was_play_state == 'playing'
    # The PlayingNow icon (index 4 of svg_info.bases) is what the user sees.
    assert result.tray.tray_icon.shown == [4]
    # A change of inhibit kind restarts swayidle so the timeouts drop out.
    assert result.tray.idle_starts == ['idle']
    assert result.tray.poll_100ms is True


def test_the_verdict_comes_from_logind_not_from_the_table(monkeypatch):
    """The table is a rendering; the verdict must not depend on it.

    Here the table is empty and the answer is still right, which is the point
    of asking a property instead of parsing a listing.
    """
    result = _check(monkeypatch, SLEEP_BLOCKED, 'Paused', table='')

    assert result.tray.was_inhibited == 'systemd'
    assert result.commands[0] == [
        'busctl', 'get-property', 'org.freedesktop.login1',
        '/org/freedesktop/login1', 'org.freedesktop.login1.Manager',
        'BlockInhibited']


def test_the_table_is_not_read_when_nothing_blocks(monkeypatch):
    """Fewer processes than before: no listing when the answer is "nothing"."""
    result = _check(monkeypatch, NOTHING_BLOCKED, 'Paused')

    assert [cmd[0] for cmd in result.commands] == ['busctl', 'playerctl']


def test_paused_media_does_not_inhibit(monkeypatch):
    result = _check(monkeypatch, NOTHING_BLOCKED, 'Paused')

    assert result.tray.was_inhibited == ''      # cleared, not left as None
    assert result.tray.tray_icon.shown == [0]   # SettingSun, the normal icon
    assert result.tray.idle_starts == ['idle']


@pytest.mark.parametrize('blocked', [SLEEP_BLOCKED, IDLE_BLOCKED,
                                     SLEEP_AND_IDLE,
                                     's "handle-power-key:sleep"\n'])
def test_sleep_or_idle_blocked_is_an_inhibitor(monkeypatch, blocked):
    result = _check(monkeypatch, blocked, 'Paused')

    assert result.tray.was_inhibited == 'systemd'
    assert result.tray.tray_icon.shown == [7]   # StopSign


@pytest.mark.parametrize('blocked', [POWER_KEY_ONLY, SHUTDOWN_BLOCKED,
                                     LID_SWITCH_BLOCKED])
def test_an_input_event_is_not_an_inhibitor(monkeypatch, blocked):
    """xfce4/KDE/niri own the power or lid key; that is not an inhibitor.

    The old code needed a list of WHO strings, matched by substring against a
    translated field, to know this.  The operation names say it directly.
    """
    result = _check(monkeypatch, blocked, 'Playing')

    assert result.rows == []
    assert result.tray.was_inhibited == 'player'
    assert result.tray.tray_icon.shown == [4]


def test_a_real_inhibitor_outranks_media_playback(monkeypatch):
    """A logind inhibitor already has the StopSign icon; keep it."""
    result = _check(monkeypatch, SLEEP_BLOCKED, 'Playing',
                    table=INHIBITOR_TABLE)

    assert result.tray.was_inhibited == 'systemd'
    assert result.tray.tray_icon.shown == [7]
    assert result.rows[0] == 'blocked: sleep'
    # The row that blocks sleep is shown; the power-key row is not.
    assert any('firefox' in row for row in result.rows)
    assert not any('niri' in row for row in result.rows)


def test_an_unreadable_table_costs_rows_not_the_verdict(monkeypatch):
    result = _check(monkeypatch, SLEEP_BLOCKED, 'Paused',
                    failures={'systemd-inhibit': (1, 'no such file')})

    assert result.tray.was_inhibited == 'systemd'
    assert result.rows == ['blocked: sleep']


def test_a_failed_query_warns_once_and_clears_the_inhibit(monkeypatch):
    """Unknown is not inhibited -- but it is never silent.

    The applet exists to lock the screen, so an unreadable inhibit state must
    not switch locking off; it must also not go unmentioned in the log.
    """
    logged = []
    monkeypatch.setattr('pwr_tray.main.prt',
                        lambda *args, **_kwargs: logged.append(str(args[0])))
    tray = _tray()
    monkeypatch.setattr(
        'pwr_tray.main.subprocess.run',
        _run_stub([], '', failures={'busctl': (1, 'Failed to connect')}))

    for _ in range(3):
        assert tray.check_inhibited()[0] == []

    assert tray.was_inhibited == ''
    warnings = [line for line in logged if 'busctl' in line]
    assert len(warnings) == 1       # once, not once per event
    assert warnings[0].startswith('WARN')


def test_an_unreadable_answer_is_unknown_not_empty(monkeypatch):
    logged = []
    monkeypatch.setattr('pwr_tray.main.prt',
                        lambda *args, **_kwargs: logged.append(str(args[0])))
    tray = _tray()
    monkeypatch.setattr('pwr_tray.main.subprocess.run',
                        _run_stub([], 'garbage\n'))

    assert tray.check_inhibited()[0] == []
    assert tray.was_inhibited == ''
    assert any('unexpected' in line for line in logged)


def test_playerctl_is_not_queried_when_the_feature_is_off(monkeypatch):
    """The menu toggle must actually stop the query and the inhibit."""
    result = _check(monkeypatch, NOTHING_BLOCKED, 'Playing',
                    enable_playerctl=False)

    assert [cmd[0] for cmd in result.commands] == ['busctl']
    assert result.tray.was_inhibited == ''
    assert result.tray.was_play_state == ''


def test_without_playerctl_the_play_state_stays_empty(monkeypatch):
    result = _check(monkeypatch, NOTHING_BLOCKED, 'Playing',
                    has_playerctl=False)

    assert [cmd[0] for cmd in result.commands] == ['busctl']
    assert result.tray.was_inhibited == ''


# -- SwayIdleManager.build_cmd(): the decision is consumed here -------------

LOCKER = 'swaylock'
BLANK_OFF = 'wlopm --off \\*'
BLANK_ON = 'wlopm --on \\*'


def _applet(mode='SleepAfterLock', inhibited='', quick=False,
            turn_off_monitors=False):
    """The slice of PwrTray that SwayIdleManager reads."""
    return SimpleNamespace(
        variables={'locker': LOCKER, 'monitors_off': BLANK_OFF,
                   'monitors_on': BLANK_ON},
        was_inhibited=inhibited,
        quick=quick,
        get_effective_mode=lambda: mode,
        get_lock_min_list=lambda selector=None: [15, 30],
        get_sleep_min_list=lambda selector=None: [5, 30],
        get_params=lambda selector=None: SimpleNamespace(
            turn_off_monitors=turn_off_monitors),
    )


@pytest.fixture
def manager(monkeypatch):
    """Build a SwayIdleManager; its only outside contact (pgrep/pkill) is stubbed."""
    monkeypatch.setattr(SwayIdleManager, 'kill_other_swayidle',
                        staticmethod(lambda: None))
    monkeypatch.setattr(sway_module, 'prt', lambda *args, **kwargs: None)

    def build(**kwargs):
        return SwayIdleManager(_applet(**kwargs))
    return build


def test_idle_timeouts_are_passed_while_nothing_plays(manager):
    idle = manager(inhibited='')

    assert idle.build_cmd() is True
    assert "timeout 900 'swaylock'" in idle.current_cmd       # 15 min
    assert "timeout 1200 'systemctl suspend'" in idle.current_cmd
    # Placeholders are substituted, not shipped to the shell.
    assert '[lock_s]' not in idle.current_cmd
    assert '[sleep_s]' not in idle.current_cmd


def test_media_playback_removes_the_idle_timeouts(manager):
    """v1 #1 on Wayland: no timeouts is how the inhibit takes effect."""
    idle = manager(inhibited='player')
    assert idle.build_cmd() is True

    assert 'timeout' not in idle.current_cmd
    assert 'systemctl suspend' not in idle.current_cmd
    assert '[lock_s]' not in idle.current_cmd
    assert idle.current_cmd.startswith('exec swayidle')


def test_media_playback_also_skips_blanking(manager):
    idle = manager(inhibited='player', turn_off_monitors=True)
    idle.build_cmd()

    assert 'timeout' not in idle.current_cmd
    # after-resume is only emitted together with the blank timeout.
    assert 'after-resume' not in idle.current_cmd


def test_blanking_is_kept_when_nothing_plays(manager):
    idle = manager(inhibited='', turn_off_monitors=True)
    idle.build_cmd()

    assert "timeout 920 'wlopm --off \\*'" in idle.current_cmd   # 900 + 20
    assert 'after-resume' in idle.current_cmd


def test_build_cmd_reports_only_actual_changes(manager):
    idle = manager(inhibited='')

    assert idle.build_cmd() is True
    assert idle.build_cmd() is False

    # Playback starts: the command changes again, so swayidle gets restarted.
    idle.applet.was_inhibited = 'player'
    assert idle.build_cmd() is True
    assert 'timeout' not in idle.current_cmd
