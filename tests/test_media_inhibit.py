"""Regression tests for the media-inhibit path (audit finding R2, v1 #1).

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

# Realistic enough for the parser: a header row, then one blocked action.
NO_INHIBITORS = 'No inhibitors listed. (--no-pager for a detailed list)\n'
SYSTEMD_INHIBIT = (
    'WHO UID USER PID  COMM     WHAT  WHY    MODE\n'
    'joe 1000 joe 4242 firefox  idle  video  block\n'
)


def _power_key_row(comm, what):
    """A single power-manager row: only the lid/power key is inhibited."""
    return ('WHO UID USER PID  COMM WHAT WHY MODE\n'
            f'joe 1000 joe 4242 {comm} {what} 1 block\n')


class _Completed:
    """The subset of CompletedProcess that check_inhibited() reads."""

    def __init__(self, stdout=''):
        self.stdout = stdout
        self.returncode = 0


def _run_stub(commands, inhibit_output, play_state):
    """A subprocess.run that records argv and answers the two queries made."""
    def run(argv, **_kwargs):
        commands.append(list(argv))
        if argv[0] == 'systemd-inhibit':
            return _Completed(inhibit_output)
        if argv[0] == 'playerctl':
            return _Completed(f'{play_state}\n')
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


def _check(monkeypatch, inhibit_output, play_state, **tray_kwargs):
    tray = _tray(**tray_kwargs)
    commands = []
    monkeypatch.setattr(
        'pwr_tray.main.subprocess.run',
        _run_stub(commands, inhibit_output, play_state))
    rows, updated = tray.check_inhibited()
    return SimpleNamespace(tray=tray, rows=rows, updated=updated,
                           commands=commands)


# -- check_inhibited(): playerctl -> inhibited='player' ---------------------

def test_playing_media_sets_the_player_inhibit(monkeypatch):
    """v1 #1: the play state must reach was_inhibited, not just be read."""
    result = _check(monkeypatch, NO_INHIBITORS, 'Playing')

    assert result.tray.was_inhibited == 'player'
    assert result.rows == []
    assert result.updated is True            # the menu shows the new rows
    assert result.tray.was_play_state == 'playing'
    # The PlayingNow icon (index 4 of svg_info.bases) is what the user sees.
    assert result.tray.tray_icon.shown == [4]
    # A change of inhibit kind restarts swayidle so the timeouts drop out.
    assert result.tray.idle_starts == ['idle']
    assert result.tray.poll_100ms is True


def test_the_inhibit_query_is_the_one_the_parser_expects(monkeypatch):
    result = _check(monkeypatch, NO_INHIBITORS, 'Playing')

    assert result.commands == [['systemd-inhibit', '--no-pager', '--mode=block'],
                               ['playerctl', 'status']]


def test_paused_media_does_not_inhibit(monkeypatch):
    result = _check(monkeypatch, NO_INHIBITORS, 'Paused')

    assert result.tray.was_inhibited == ''      # cleared, not left as None
    assert result.tray.tray_icon.shown == [0]   # SettingSun, the normal icon
    assert result.tray.idle_starts == ['idle']


def test_a_real_inhibitor_outranks_media_playback(monkeypatch):
    """A logind inhibitor already has the StopSign icon; keep it."""
    result = _check(monkeypatch, SYSTEMD_INHIBIT, 'Playing')

    assert result.tray.was_inhibited == 'systemd'
    assert len(result.rows) == 2       # header + the blocking row
    assert result.tray.tray_icon.shown == [7]


@pytest.mark.parametrize('comm,what', [
    ('xfce4-power-man', 'handle-power-key:handle-lid-switch'),
    ('org_kde_powerde', 'handle-power-key'),
    ('niri', 'handle-power-key:handle-suspend-key'),
])
def test_a_power_key_row_does_not_suppress_media_inhibit(monkeypatch, comm, what):
    """xfce4/KDE/niri own only the lid or power key; they are not inhibitors."""
    result = _check(monkeypatch, _power_key_row(comm, what), 'Playing')

    assert result.rows == []
    assert result.tray.was_inhibited == 'player'
    assert result.tray.tray_icon.shown == [4]


def test_playerctl_is_not_queried_when_the_feature_is_off(monkeypatch):
    """The menu toggle must actually stop the query and the inhibit."""
    result = _check(monkeypatch, NO_INHIBITORS, 'Playing',
                    enable_playerctl=False)

    assert [cmd[0] for cmd in result.commands] == ['systemd-inhibit']
    assert result.tray.was_inhibited == ''
    assert result.tray.was_play_state == ''


def test_without_playerctl_the_play_state_stays_empty(monkeypatch):
    result = _check(monkeypatch, NO_INHIBITORS, 'Playing',
                    has_playerctl=False)

    assert [cmd[0] for cmd in result.commands] == ['systemd-inhibit']
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
