"""Event sources replace the old once-per-tick polling (AUDIT.md finding 10).

Two things are pinned here:

- a signal only marks the matching input dirty when it is actually relevant
  (a metadata change in a player must not look like a play/pause change), and
- ``on_timeout()`` runs the expensive queries only when something is dirty.

The second test builds a bare ``PwrTray`` (no ``__init__``) because the point
is the gating decision, not Qt or D-Bus.
"""

from types import SimpleNamespace

import pwr_tray.PowerEvents as events_module
from pwr_tray.main import PwrTray
from pwr_tray.PowerEvents import PowerEvents


def _events():
    calls = []
    watcher = PowerEvents(
        on_inhibitors=lambda: calls.append('inhibitors'),
        on_battery=lambda: calls.append('battery'),
        on_playback=lambda: calls.append('playback'),
        on_config=lambda: calls.append('config'))
    return watcher, calls


# -- dbus-monitor parsing ---------------------------------------------------

def _header(path):
    return ('signal time=1 sender=:1.2 -> destination=(null destination) '
            f'serial=2 path={path}; interface=org.freedesktop.DBus.Properties;'
            ' member=PropertiesChanged')


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


def _bare_tray(bus_ok=True, config_ok=True):
    """A PwrTray stripped down to what on_timeout() touches at loop < sample."""
    calls = []
    tray = object.__new__(PwrTray)
    tray.loop = 0
    tray.loop_sample = 15
    tray.quick = False
    tray.poll_s = 2.0
    tray.poll_100ms = False
    tray._refresh_pending = False
    tray._config_dirty = False
    tray._battery_dirty = False
    tray._inhibit_dirty = False
    tray._inhibit_rows = []
    tray._events_bus_ok = bus_ok
    tray._events_config_ok = config_ok
    tray.rebuild_menu = False
    tray.was_inhibited = None
    tray.idle_manager = None
    tray.timer = _FakeTimer()
    tray.battery = SimpleNamespace(selector='Settings', percent=90,
                                   plugged=True, present=True)
    tray.DB = lambda: False
    tray._check_resume = lambda: calls.append('resume')
    tray.reconfig = lambda: calls.append('reconfig')
    tray.update_battery_status = lambda: calls.append('battery')

    def check_inhibited():
        calls.append('inhibited')
        return ['row'], True

    tray.check_inhibited = check_inhibited
    tray.show_icon = lambda inhibited='': None
    tray.build_menu = lambda rows=None: calls.append(('menu', rows))
    return tray, calls


def test_quiet_tick_spawns_no_queries():
    tray, calls = _bare_tray()

    tray.on_timeout()

    assert calls == ['resume']
    assert tray._refresh_pending is False


def test_dirty_inhibitors_are_queried_once():
    tray, calls = _bare_tray()
    tray._inhibit_dirty = True

    tray.on_timeout()

    assert calls == ['resume', 'inhibited', ('menu', ['row'])]
    assert tray._inhibit_rows == ['row']


def test_dirty_battery_is_queried_once():
    tray, calls = _bare_tray()
    tray._battery_dirty = True

    tray.on_timeout()

    assert calls == ['resume', 'battery']


def test_dirty_config_is_reread_once():
    tray, calls = _bare_tray()
    tray._config_dirty = True

    tray.on_timeout()

    assert calls == ['resume', 'reconfig']


def test_missing_event_bus_falls_back_to_polling():
    tray, calls = _bare_tray(bus_ok=False)

    tray.on_timeout()

    assert 'battery' in calls
    assert 'inhibited' in calls


def test_missing_config_watch_falls_back_to_polling():
    tray, calls = _bare_tray(config_ok=False)

    tray.on_timeout()

    assert 'reconfig' in calls


# -- coalescing -------------------------------------------------------------

def test_events_coalesce_into_one_fast_poll():
    tray, _ = _bare_tray()

    tray._on_inhibitors_event()
    tray._on_battery_event()
    tray._on_playback_event()
    tray._on_config_event()

    assert tray._inhibit_dirty is True
    assert tray._battery_dirty is True
    assert tray._config_dirty is True
    assert tray.timer.intervals == [100]


def test_playback_event_marks_inhibitors_dirty():
    """The icon and swayidle command depend on the media play state."""
    tray, _ = _bare_tray()

    tray._on_playback_event()

    assert tray._inhibit_dirty is True
    assert tray.timer.intervals == [100]


def test_resync_marks_both_slow_inputs_dirty():
    tray, _ = _bare_tray()

    tray._resync()

    assert tray._inhibit_dirty is True
    assert tray._battery_dirty is True
