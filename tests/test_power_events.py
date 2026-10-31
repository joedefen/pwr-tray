"""Event sources replace the old once-per-tick polling, and carry the values.

Four things are pinned here:

- a signal only marks the matching input dirty when it is actually relevant
  (a metadata change in a player must not look like a play/pause change),
- the signal's *value* is kept, so the tick does not have to ask a program for
  it (``BlockInhibited``, MPRIS ``PlaybackStatus``, player lifetime),
- ``on_timeout()`` runs the expensive queries only when something is dirty,
- once the slow branch is due, ``power_down`` really selects ``poweroff()``
  (the comparison was once against the wrong string, so it always suspended).

The tests build a bare ``PwrTray`` (no ``__init__``) because the point is the
gating/decision logic, not Qt or D-Bus.
"""

import sys
import types
from types import SimpleNamespace

import pwr_tray.PowerEvents as events_module
from pwr_tray.IdleStateMachine import IdleStateMachine
from pwr_tray.main import PwrTray
from pwr_tray.PowerEvents import PowerEvents


def _events():
    calls = []
    watcher = PowerEvents(
        on_inhibitors=lambda: calls.append('inhibitors'),
        on_battery=lambda: calls.append('battery'),
        on_playback=lambda: calls.append('playback'),
        on_config=lambda: calls.append('config'),
        on_resume=lambda: calls.append('resume'))
    return watcher, calls


# -- dbus-monitor parsing ---------------------------------------------------

def _header(path, sender=':1.2'):
    return (f'signal time=1 sender={sender} -> destination=(null destination) '
            f'serial=2 path={path}; interface=org.freedesktop.DBus.Properties;'
            ' member=PropertiesChanged')


def _owner_header():
    return ('signal time=3 sender=org.freedesktop.DBus -> destination='
            '(null destination) serial=4 path=/org/freedesktop/DBus;'
            ' interface=org.freedesktop.DBus; member=NameOwnerChanged')


def _change(prop, value, interface='org.freedesktop.login1.Manager'):
    """A PropertiesChanged body: one property, with the value it changed to."""
    return [f'   string "{interface}"',
            '   array [',
            '      dict entry(',
            f'         string "{prop}"',
            f'         variant             string "{value}"',
            '      )',
            '   ]',
            '   array [',
            '   ]']


def _feed(watcher, key, *lines):
    for line in lines:
        watcher._scan_line(key, line)


def test_logind_inhibitor_properties_trigger_a_refresh():
    watcher, calls = _events()

    _feed(watcher, 'system',
          _header('/org/freedesktop/login1'),
          '   string "org.freedesktop.login1.Manager"',
          '         string "BlockInhibited"',
          '         string "DelayInhibited"')

    # One message, one callback -- not one per changed property.
    assert calls == ['inhibitors']


def test_unrelated_logind_properties_are_ignored():
    watcher, calls = _events()

    _feed(watcher, 'system',
          _header('/org/freedesktop/login1'),
          '   string "org.freedesktop.login1.Manager"',
          '         string "IdleHint"')

    assert calls == []


def test_monitor_traffic_from_other_paths_is_ignored():
    """dbus-monitor also sees its own NameAcquired signal."""
    watcher, calls = _events()

    _feed(watcher, 'system',
          _header('/org/freedesktop/DBus'),
          '   string ":1.0"')

    assert calls == []


def test_upower_ac_and_device_changes_trigger_a_refresh():
    watcher, calls = _events()

    _feed(watcher, 'system',
          _header('/org/freedesktop/UPower'),
          '   string "org.freedesktop.UPower"',
          '         string "OnBattery"')
    _feed(watcher, 'system',
          _header('/org/freedesktop/UPower/devices/battery_BAT0'),
          '   string "org.freedesktop.UPower.Device"',
          '         string "Percentage"')

    assert calls == ['battery', 'battery']


def test_upower_unrelated_properties_are_ignored():
    watcher, calls = _events()

    _feed(watcher, 'system',
          _header('/org/freedesktop/UPower/devices/battery_BAT0'),
          '         string "UpdateTime"')
    _feed(watcher, 'system',
          _header('/org/freedesktop/UPower'),
          '         string "DaemonVersion"')

    assert calls == []


def test_mpris_only_reacts_to_playback_status():
    watcher, calls = _events()

    _feed(watcher, 'session',
          _header('/org/mpris/MediaPlayer2'),
          '   string "org.mpris.MediaPlayer2.Player"',
          '         string "PlaybackStatus"')
    _feed(watcher, 'session',
          _header('/org/mpris/MediaPlayer2'),
          '         string "Metadata"')

    assert calls == ['playback']


# -- the values are kept ----------------------------------------------------

def test_block_inhibited_value_is_kept():
    """The signal already says which operations are blocked."""
    watcher, calls = _events()

    _feed(watcher, 'system', _header('/org/freedesktop/login1'),
          *_change('BlockInhibited', 'sleep:idle'))

    assert watcher.blocked == frozenset({'sleep', 'idle'})
    assert calls == ['inhibitors']


def test_an_empty_block_inhibited_value_is_an_answer_not_unknown():
    watcher, _ = _events()

    _feed(watcher, 'system', _header('/org/freedesktop/login1'),
          *_change('BlockInhibited', ''))

    assert watcher.blocked == frozenset()
    assert watcher.blocked is not None


def test_a_session_block_inhibited_does_not_overwrite_the_manager_value():
    """Manager and session scope answer different questions."""
    watcher, _ = _events()
    _feed(watcher, 'system', _header('/org/freedesktop/login1'),
          *_change('BlockInhibited', 'sleep'))
    _feed(watcher, 'system', _header('/org/freedesktop/login1/session/_32'),
          *_change('BlockInhibited', ''))

    assert watcher.blocked == frozenset({'sleep'})


def test_an_invalidated_property_returns_to_unknown():
    """No value in the message means the cached answer cannot be trusted."""
    watcher, calls = _events()
    _feed(watcher, 'system', _header('/org/freedesktop/login1'),
          *_change('BlockInhibited', 'sleep'))
    assert watcher.blocked == frozenset({'sleep'})

    # The property is named but never followed by a variant: invalidated.
    _feed(watcher, 'system', _header('/org/freedesktop/login1'),
          '         string "BlockInhibited"')
    # The invalidated form is only knowable when the message ends.
    _feed(watcher, 'system', _header('/org/freedesktop/login1'))

    assert watcher.blocked is None
    assert calls == ['inhibitors', 'inhibitors']


def test_playback_status_is_kept_per_player():
    watcher, calls = _events()

    _feed(watcher, 'session', _header('/org/mpris/MediaPlayer2', ':1.9'),
          *_change('PlaybackStatus', 'Playing',
                   interface='org.mpris.MediaPlayer2.Player'))

    assert watcher.media_playing is True
    assert calls == ['playback']


def test_any_playing_player_keeps_the_session_awake():
    watcher, _ = _events()
    _feed(watcher, 'session', _header('/org/mpris/MediaPlayer2', ':1.9'),
          *_change('PlaybackStatus', 'Playing',
                   interface='org.mpris.MediaPlayer2.Player'))
    _feed(watcher, 'session', _header('/org/mpris/MediaPlayer2', ':2.0'),
          *_change('PlaybackStatus', 'Paused',
                   interface='org.mpris.MediaPlayer2.Player'))
    assert watcher.media_playing is True

    # The playing player pauses: still a real False, not "unknown".
    _feed(watcher, 'session', _header('/org/mpris/MediaPlayer2', ':1.9'),
          *_change('PlaybackStatus', 'Paused',
                   interface='org.mpris.MediaPlayer2.Player'))
    assert watcher.media_playing is False


def test_a_player_leaving_the_bus_is_forgotten():
    watcher, calls = _events()
    _feed(watcher, 'session', _header('/org/mpris/MediaPlayer2', ':1.9'),
          *_change('PlaybackStatus', 'Playing',
                   interface='org.mpris.MediaPlayer2.Player'))

    _feed(watcher, 'session', _owner_header(),
          '   string "org.mpris.MediaPlayer2.firefox.instance42"',
          '   string ":1.9"',
          '   string ""')

    assert watcher.media_playing is False
    assert calls == ['playback', 'playback']


def test_a_new_player_is_unknown_until_it_reports():
    """A player that never publishes a status must still be asked once."""
    watcher, calls = _events()

    _feed(watcher, 'session', _owner_header(),
          '   string "org.mpris.MediaPlayer2.firefox.instance42"',
          '   string ""',
          '   string ":1.9"')

    assert watcher.media_playing is None
    assert calls == ['playback']


def test_non_mpris_name_changes_are_ignored():
    watcher, calls = _events()

    _feed(watcher, 'session', _owner_header(),
          '   string "org.kde.StatusNotifierWatcher"',
          '   string ""',
          '   string ":1.9"')

    assert calls == []
    assert watcher.media_playing is None


# -- who is playing --------------------------------------------------------

def _playing(watcher, sender=':1.9', status='Playing'):
    _feed(watcher, 'session', _header('/org/mpris/MediaPlayer2', sender),
          *_change('PlaybackStatus', status,
                   interface='org.mpris.MediaPlayer2.Player'))


def _appears(watcher, well_known, sender):
    _feed(watcher, 'session', _owner_header(),
          f'   string "{well_known}"',
          '   string ""',
          f'   string "{sender}"')


def test_the_player_name_is_learned_from_the_owner_change():
    """A unique bus name is not something to put in a menu."""
    watcher, _ = _events()
    _appears(watcher, 'org.mpris.MediaPlayer2.firefox.instance3217', ':1.9')
    _playing(watcher)

    assert watcher.media_players == ['firefox']


def test_an_instance_suffix_is_not_part_of_the_name():
    for well_known, expected in (
            ('org.mpris.MediaPlayer2.vlc', 'vlc'),
            ('org.mpris.MediaPlayer2.chromium.instance1234', 'chromium')):
        watcher, _ = _events()
        _appears(watcher, well_known, ':1.9')
        _playing(watcher)
        assert watcher.media_players == [expected]


def test_only_playing_players_are_named():
    watcher, _ = _events()
    _appears(watcher, 'org.mpris.MediaPlayer2.firefox.instance3217', ':1.9')
    _playing(watcher, status='Paused')

    assert watcher.media_players == []


def test_an_unnamed_player_is_still_counted():
    """A player that appeared without a name we saw is not dropped."""
    watcher, _ = _events()
    _playing(watcher, sender=':2.0')

    assert watcher.media_players == ['player']


def test_a_player_name_is_forgotten_when_it_leaves():
    watcher, _ = _events()
    _appears(watcher, 'org.mpris.MediaPlayer2.firefox.instance3217', ':1.9')
    _playing(watcher)

    _feed(watcher, 'session', _owner_header(),
          '   string "org.mpris.MediaPlayer2.firefox.instance3217"',
          '   string ":1.9"',
          '   string ""')

    assert watcher.media_players == []


def _fake_qtbus(monkeypatch, services):
    """A PyQt5.QtDBus just real enough for _seed_players()."""
    module = types.ModuleType('PyQt5.QtDBus')

    class _Reply:
        def __init__(self, value):
            self._value = value

        def value(self):
            return self._value

    class _Interface:
        @staticmethod
        def registeredServiceNames():
            return _Reply(list(services))

        @staticmethod
        def serviceOwner(name):
            return _Reply(services[name])

    class _Bus:
        @staticmethod
        def isConnected():
            return True

        @staticmethod
        def interface():
            return _Interface()

    class _Connection:
        @staticmethod
        def sessionBus():
            return _Bus()

    module.QDBusConnection = _Connection
    monkeypatch.setitem(sys.modules, 'PyQt5.QtDBus', module)


def test_seeding_names_a_player_that_was_already_on_the_bus(monkeypatch):
    """No NameOwnerChanged arrives for a player that predates the applet."""
    _fake_qtbus(monkeypatch, {'org.mpris.MediaPlayer2.spotify': ':1.7',
                              'org.freedesktop.DBus': ':1.0'})
    watcher, _ = _events()
    watcher._seed_players()
    _playing(watcher, sender=':1.7')

    assert watcher.media_players == ['spotify']


def test_seeding_without_qtdbus_is_a_noop(monkeypatch):
    monkeypatch.setitem(sys.modules, 'PyQt5.QtDBus', None)
    watcher, _ = _events()

    watcher._seed_players()

    assert watcher.media_players == []


# -- the one-off query path -------------------------------------------------

def test_a_one_off_query_can_seed_the_cache_and_is_invalidated():
    watcher, _ = _events()
    assert watcher.blocked is None
    assert watcher.media_playing is None

    watcher.note_blocked({'sleep'})
    watcher.note_media(True)
    assert watcher.blocked == frozenset({'sleep'})
    assert watcher.media_playing is True

    watcher.invalidate()
    assert watcher.blocked is None
    assert watcher.media_playing is None


def test_a_failed_one_off_query_leaves_the_answer_unknown():
    watcher, _ = _events()

    watcher.note_blocked(None)

    assert watcher.blocked is None


# -- graceful degradation ---------------------------------------------------

def test_start_without_dbus_or_watcher_is_a_noop(monkeypatch):
    monkeypatch.setattr(events_module, 'QSocketNotifier', None)
    monkeypatch.setattr(events_module, 'QFileSystemWatcher', None)
    watcher, _ = _events()

    assert watcher.start('/tmp/does-not-matter.ini') is False
    assert watcher.bus_ok is False
    assert watcher.config_ok is False


# -- on_timeout gating ------------------------------------------------------

class _FakeTimer:
    def __init__(self):
        self.intervals = []

    def start(self, milliseconds):
        self.intervals.append(milliseconds)

    def setInterval(self, milliseconds):
        self.intervals.append(milliseconds)


class _Events:
    """The slice of PowerEvents the applet reads."""

    def __init__(self, blocked=frozenset(), playing=False):
        self.blocked = blocked
        self.media_playing = playing
        self.invalidated = False

    def invalidate(self):
        self.invalidated = True
        self.blocked = None
        self.media_playing = None

    def note_blocked(self, blocked):    # pragma: no cover - not reached here
        self.blocked = frozenset(blocked)

    def note_media(self, playing):      # pragma: no cover - not reached here
        self.media_playing = playing


def _bare_tray(bus_ok=True, config_ok=True, inhibit=((), False)):
    """A PwrTray stripped down to what on_timeout() touches at loop < sample."""
    calls = []
    tray = object.__new__(PwrTray)
    # The class delegates the slow branch to this collaborator; give the bare
    # instance the real one so the tick tests exercise the shipped logic rather
    # than a stand-in.
    tray.idle_state = IdleStateMachine(tray)
    tray.loop = 0
    tray.loop_sample = 15
    tray.quick = False
    tray.poll_s = 2.0
    tray.poll_100ms = False
    tray._refresh_pending = False
    tray._config_dirty = False
    tray._battery_dirty = False
    tray._inhibit_rows = []
    tray._rows_blocked = None
    tray.events = None
    tray._events_bus_ok = bus_ok
    tray._events_config_ok = config_ok
    tray.rebuild_menu = False
    tray.was_inhibited = None
    tray.idle_manager = None
    tray.timer = _FakeTimer()
    tray.battery = SimpleNamespace(selector='Settings', percent=90,
                                   plugged=True, present=True)
    tray.DB = lambda: False
    tray.reconfig = lambda: calls.append('reconfig')
    tray.update_battery_status = lambda: calls.append('battery')
    tray._ensure_tray_attached = lambda: calls.append('tray')

    rows, updated = inhibit

    def check_inhibited():
        calls.append('inhibited')
        return list(rows), updated

    tray.check_inhibited = check_inhibited
    tray.show_icon = lambda inhibited='': None
    tray.build_menu = lambda rows=None: calls.append(('menu', rows))
    return tray, calls


def test_a_tick_reads_the_verdict_without_a_dirty_flag():
    """There is no flag to miss: the verdict is read on every tick."""
    tray, calls = _bare_tray()

    tray.on_timeout()

    assert calls == ['inhibited']


def test_an_unchanged_verdict_does_not_rebuild_the_menu():
    tray, calls = _bare_tray(inhibit=(['row'], False))

    tray.on_timeout()

    assert calls == ['inhibited']
    assert tray._inhibit_rows == ['row']


def test_a_changed_verdict_rebuilds_the_menu_once():
    tray, calls = _bare_tray(inhibit=(['row'], True))

    tray.on_timeout()

    assert calls == ['inhibited', ('menu', ['row'])]
    assert tray.rebuild_menu is False


def test_dirty_battery_is_queried_once():
    tray, calls = _bare_tray()
    tray._battery_dirty = True

    tray.on_timeout()

    assert calls == ['battery', 'inhibited']


def test_dirty_config_is_reread_once():
    tray, calls = _bare_tray()
    tray._config_dirty = True

    tray.on_timeout()

    assert calls == ['reconfig', 'inhibited']


def test_missing_event_bus_falls_back_to_polling():
    tray, calls = _bare_tray(bus_ok=False)

    tray.on_timeout()

    assert 'battery' in calls
    assert 'inhibited' in calls


def test_missing_config_watch_falls_back_to_polling():
    tray, calls = _bare_tray(config_ok=False)

    tray.on_timeout()

    assert 'reconfig' in calls


# -- down state -------------------------------------------------------------

def _bare_due_tray(power_down):
    """The harness above with the tick due for its once-per-sample branch."""
    tray, calls = _bare_tray()
    tray.loop = tray.loop_sample - 1     # this tick reaches loop_sample
    tray.running_idle_s = 3600.0
    tray.state = SimpleNamespace(name='Awake', when=0)
    tray.get_effective_mode = lambda: 'SleepAfterLock'
    tray.get_lock_min_list = lambda selector=None: [15, 30]
    tray.get_sleep_min_list = lambda selector=None: [5, 30]
    params = SimpleNamespace(power_down=power_down, turn_off_monitors=False)
    tray.get_params = lambda selector=None: params
    tray.update_running_idle_s = lambda: None
    tray.reset_xidle_ms = lambda: calls.append('reset')
    tray.poweroff = lambda _: calls.append('poweroff')
    tray.suspend = lambda _: calls.append('suspend')
    tray.lock_screen = lambda _: calls.append('lock')
    tray.blank_primitive = lambda: calls.append('blank')
    tray.set_state = lambda name: calls.append(('state', name))
    return tray, calls


def test_power_down_preference_calls_poweroff():
    """v1 #3: power_down = True suspended instead of powering off."""
    tray, calls = _bare_due_tray(power_down=True)

    tray.on_timeout()

    assert calls == ['inhibited', 'poweroff']


def test_power_down_unset_still_suspends():
    tray, calls = _bare_due_tray(power_down=False)

    tray.on_timeout()

    assert calls == ['inhibited', 'suspend']


# -- coalescing -------------------------------------------------------------

def test_events_coalesce_into_one_fast_poll():
    tray, _ = _bare_tray()

    tray._on_inhibitors_event()
    tray._on_battery_event()
    tray._on_playback_event()
    tray._on_config_event()

    assert tray._battery_dirty is True
    assert tray._config_dirty is True
    assert tray.timer.intervals == [100]


def test_a_playback_event_schedules_a_tick():
    """The icon and the swayidle command depend on the media play state."""
    tray, _ = _bare_tray()

    tray._on_playback_event()

    assert tray.timer.intervals == [100]


def test_resume_event_forces_the_slow_branch():
    """PrepareForSleep(false) is handled by the event source, not a poll."""
    tray, _ = _bare_tray()

    tray._on_resume_event()

    assert tray.poll_100ms is True
    assert tray.timer.intervals == [100]


def test_resync_invalidates_the_cached_answers():
    """The safety net exists because a cache can miss a signal."""
    tray, calls = _bare_tray()
    tray.events = _Events(blocked={'sleep'}, playing=True)

    tray._resync()

    assert tray.events.invalidated is True
    assert tray._battery_dirty is True
    assert calls == ['tray']
    assert tray.timer.intervals == [100]
