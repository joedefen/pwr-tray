"""The battery reading comes from the UPower payload, not from psutil.

``psutil.sensors_battery()`` used to be called on every battery event.  UPower
publishes ``Percentage``, ``State``, ``IsPresent`` and ``OnBattery`` as D-Bus
property changes whose payload *is* the new value, so the event source keeps
the reading and psutil becomes the fallback: the first tick, a machine without
UPower, and the five-minute resync.

Two things are pinned here:

- the payload's *type*.  logind and MPRIS publish strings, UPower publishes
  booleans and numbers (``variant double 87.5``), so the signature on the line
  has to pick the parser -- one string parser would silently keep nothing;
- that a bus which has answered costs no psutil call at all, including after
  it has answered only once.
"""

from types import SimpleNamespace

import pwr_tray.main as main_module
from pwr_tray.main import PwrTray
from pwr_tray.PowerEvents import PowerEvents

UPOWER = '/org/freedesktop/UPower'
BAT0 = '/org/freedesktop/UPower/devices/battery_BAT0'
BAT1 = '/org/freedesktop/UPower/devices/battery_BAT1'
DISPLAY = '/org/freedesktop/UPower/devices/DisplayDevice'
MANAGER = 'org.freedesktop.UPower'
DEVICE = 'org.freedesktop.UPower.Device'


# -- feeding the scanner ----------------------------------------------------

def _header(path, sender=':1.2'):
    return (f'signal time=1 sender={sender} -> destination=(null destination) '
            f'serial=2 path={path}; interface=org.freedesktop.DBus.Properties;'
            ' member=PropertiesChanged')


def _body(interface, pairs):
    """A PropertiesChanged body: (property, signature, payload) triples."""
    lines = [f'   string "{interface}"', '   array [']
    for prop, signature, payload in pairs:
        lines += ['      dict entry(',
                  f'         string "{prop}"',
                  f'         variant             {signature} {payload}',
                  '      )']
    lines += ['   ]', '   array [', '   ]']
    return lines


def _feed(watcher, path, interface, *pairs):
    for line in [_header(path), *_body(interface, pairs)]:
        watcher._scan_line('system', line)


def _invalidated(watcher, path, interface, prop):
    """A property change that carries no value -- D-Bus calls it invalidated."""
    for line in [_header(path),
                 f'   string "{interface}"',
                 '   array [',
                 '      dict entry(',
                 f'         string "{prop}"',
                 '      )',
                 '   ]']:
        watcher._scan_line('system', line)
    # The message ends when the next signal starts; that is when a property
    # that never showed a value is recognised as invalidated.
    watcher._scan_line('system', _header(UPOWER))


# -- the payload types ------------------------------------------------------

def test_percentage_and_state_come_from_the_payload():
    watcher = PowerEvents()

    _feed(watcher, BAT0, DEVICE,
          ('Percentage', 'double', '87.5'),
          ('State', 'uint32', '2'))

    assert watcher.battery_percent == 87.5
    assert watcher.battery_plugged is False


def test_a_percentage_is_enough_to_know_the_battery_is_there():
    watcher = PowerEvents()

    _feed(watcher, BAT0, DEVICE, ('Percentage', 'double', '55.5'))

    assert watcher.battery_present is True


def test_charging_and_full_are_both_plugged_in():
    watcher = PowerEvents()

    _feed(watcher, BAT0, DEVICE, ('State', 'uint32', '1'))
    assert watcher.battery_plugged is True

    _feed(watcher, BAT0, DEVICE, ('State', 'uint32', '4'))
    assert watcher.battery_plugged is True


def test_an_absent_device_means_no_battery():
    watcher = PowerEvents()

    _feed(watcher, BAT0, DEVICE, ('IsPresent', 'boolean', 'false'))

    assert watcher.battery_present is False
    assert watcher.battery_percent is None


def test_on_battery_is_the_answer_when_no_device_state_arrived():
    watcher = PowerEvents()

    _feed(watcher, UPOWER, MANAGER, ('OnBattery', 'boolean', 'true'))

    assert watcher.battery_plugged is False


def test_the_device_state_outranks_the_manager_flag():
    """A device that says "charging" settles it, whatever OnBattery still says."""
    watcher = PowerEvents()
    _feed(watcher, UPOWER, MANAGER, ('OnBattery', 'boolean', 'true'))

    _feed(watcher, BAT0, DEVICE, ('State', 'uint32', '1'))

    assert watcher.battery_plugged is True


def test_a_string_variant_is_not_a_numeric_value():
    """The signature picks the parser; a string is not a percentage."""
    assert PowerEvents._variant_number(
        '         variant             string "87"') is None
    assert PowerEvents._variant_number(
        '         variant             double not-a-number') is None


def test_an_unrelated_device_property_is_kept_out_of_the_reading():
    watcher = PowerEvents()

    _feed(watcher, BAT0, DEVICE, ('TimeToEmpty', 'int64', '-1'),
          ('Percentage', 'double', '42'))

    assert watcher.battery_percent == 42.0


# -- which device answers ---------------------------------------------------

def test_the_composite_device_outranks_one_physical_battery():
    """Two batteries report their own charge; DisplayDevice is their aggregate."""
    watcher = PowerEvents()
    _feed(watcher, BAT0, DEVICE, ('Percentage', 'double', '40'))

    _feed(watcher, DISPLAY, DEVICE, ('Percentage', 'double', '71'))
    _feed(watcher, BAT1, DEVICE, ('Percentage', 'double', '99'))

    assert watcher.battery_percent == 71.0


def test_a_single_battery_is_read_when_no_composite_reports():
    watcher = PowerEvents()

    _feed(watcher, BAT0, DEVICE, ('Percentage', 'double', '40'))

    assert watcher.battery_percent == 40.0


# -- unknown versus known ---------------------------------------------------

def test_an_invalidated_property_returns_to_unknown():
    """A property that arrives without a value is not a reading of 0."""
    watcher = PowerEvents()
    _feed(watcher, BAT0, DEVICE, ('Percentage', 'double', '87.5'))

    _invalidated(watcher, BAT0, DEVICE, 'Percentage')

    assert watcher.battery_percent is None


def test_a_one_off_reading_is_used_until_upower_speaks():
    watcher = PowerEvents()
    watcher.note_battery(64, True)

    assert (watcher.battery_percent, watcher.battery_plugged) == (64.0, True)

    _feed(watcher, BAT0, DEVICE,
          ('Percentage', 'double', '63.4'),
          ('State', 'uint32', '2'))

    assert (watcher.battery_percent, watcher.battery_plugged) == (63.4, False)


def test_the_resync_forgets_the_reading():
    watcher = PowerEvents()
    watcher.note_battery(64, True)
    _feed(watcher, BAT0, DEVICE, ('State', 'uint32', '2'))

    watcher.invalidate()

    assert watcher.battery_percent is None
    assert watcher.battery_plugged is None
    assert watcher.battery_present is None


# -- the applet side --------------------------------------------------------

class _Events:
    """The slice of PowerEvents the battery path reads."""

    def __init__(self, present=None, plugged=None, percent=None):
        self.battery_present = present
        self.battery_plugged = plugged
        self.battery_percent = percent
        self.noted = None

    def note_battery(self, percent, plugged):
        # Mirrors the real one: a cached reading answers like a bus reading,
        # which is what keeps a silent bus from costing a psutil call per event.
        self.noted = (percent, plugged)
        self.battery_present = True
        self.battery_plugged = plugged
        self.battery_percent = percent


def _tray(events=None, bus_ok=True):
    tray = object.__new__(PwrTray)
    tray.events = events
    tray._events_bus_ok = bus_ok
    tray.battery = SimpleNamespace(present=None, synthetic=False,
                                   plugged=True, percent=100,
                                   selector='Settings')
    tray.rebuild_menu = False
    tray.get_params = lambda: SimpleNamespace(lo_battery_pct=20)
    return tray


def _psutil(monkeypatch, percent=None, plugged=None):
    """Install a psutil reading and return the call log."""
    calls = []

    def sensors_battery():
        calls.append('psutil')
        if percent is None:
            return None
        return SimpleNamespace(percent=percent, power_plugged=plugged)

    monkeypatch.setattr(main_module.psutil, 'sensors_battery', sensors_battery)
    return calls


def test_a_battery_event_costs_no_psutil_call(monkeypatch):
    calls = _psutil(monkeypatch, 64, True)
    tray = _tray(_Events(True, False, 55.5))

    reading = tray._battery_reading()

    assert (reading.plugged, reading.percent) == (False, 55.5)
    assert calls == []


def test_the_bus_saying_there_is_no_battery_skips_psutil(monkeypatch):
    calls = _psutil(monkeypatch, 50, False)

    assert _tray(_Events(present=False))._battery_reading() is None
    assert calls == []


def test_a_silent_bus_falls_back_to_psutil_and_is_cached(monkeypatch):
    calls = _psutil(monkeypatch, 64.4, True)
    events = _Events()
    tray = _tray(events)

    reading = tray._battery_reading()
    assert reading.percent == 64.4
    assert events.noted == (64.4, True)

    # The answer is now cached, so the next event asks nothing.
    tray._battery_reading()
    assert calls == ['psutil']


def test_without_a_bus_the_battery_comes_from_psutil(monkeypatch):
    calls = _psutil(monkeypatch, 33.3, False)
    tray = _tray(None, bus_ok=False)

    reading = tray._battery_reading()

    assert (reading.plugged, reading.percent) == (False, 33.3)
    assert calls == ['psutil']


def test_a_machine_without_a_battery_reports_none(monkeypatch):
    _psutil(monkeypatch)

    assert _tray(_Events())._battery_reading() is None


def test_update_battery_status_applies_a_bus_reading(monkeypatch):
    calls = _psutil(monkeypatch, 99, True)
    tray = _tray(_Events(True, False, 15.4))

    tray.update_battery_status()

    assert tray.battery.plugged is False
    assert tray.battery.percent == 15.4
    assert tray.battery.selector == 'LoBattery'
    assert tray.rebuild_menu is True
    assert calls == []


def test_a_plugged_reading_keeps_the_settings_tier(monkeypatch):
    _psutil(monkeypatch, 50, False)
    tray = _tray(_Events(True, True, 80))

    tray.update_battery_status()

    assert tray.battery.selector == 'Settings'
    assert tray.rebuild_menu is False


def test_an_absent_battery_is_recorded_and_not_looked_for_again(monkeypatch):
    calls = _psutil(monkeypatch)
    tray = _tray(_Events())

    tray.update_battery_status()
    tray.update_battery_status()

    assert tray.battery.present is False
    assert calls == ['psutil']


def test_a_real_reading_replaces_the_test_cycler(monkeypatch):
    _psutil(monkeypatch, 50, True)
    tray = _tray(_Events(True, False, 50))
    tray.battery.synthetic = True

    tray.update_battery_status()

    assert tray.battery.synthetic is False
