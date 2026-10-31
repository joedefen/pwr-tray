"""What the applet believes about the battery, and where that belief comes from.

``psutil.sensors_battery()`` derives ``power_plugged`` from ``AC/online``
alone.  On a Chromebook that file can say ``online=1`` while the battery says
``status=discharging``, which is how a laptop running on its battery ended up
showing "Plugged In" forever.  ``PowerSupply.read_power()`` cross-checks the
two and trusts the battery; these tests pin that, both against synthetic sysfs
trees and through the applet's own ``update_battery_status()``.
"""

import types

import pytest

import pwr_tray.main as main_module
from pwr_tray import PowerSupply


def _device(root, name, **files):
    """Create one /sys/class/power_supply/<name>/ directory."""
    folder = root / name
    folder.mkdir(parents=True)
    for key, value in files.items():
        (folder / key).write_text(f'{value}\n', encoding='utf-8')
    return folder


def _mains(root, name='AC', online=1):
    return _device(root, name, type='Mains', online=online)


def _battery(root, name='BAT0', status='discharging', present=1,
             energy_now=3650, energy_full=5000, capacity=73):
    return _device(root, name, type='Battery', status=status, present=present,
                   energy_now=energy_now, energy_full=energy_full,
                   capacity=capacity)


# -- the disagreement that started all this --------------------------------

def test_a_disagreeing_mains_flag_loses_to_the_battery(tmp_path):
    """Stale ``AC/online=1`` must not make a discharging battery "plugged in"."""
    _mains(tmp_path, online=1)
    _battery(tmp_path, status='discharging')

    power = PowerSupply.read_power(tmp_path)

    assert power.plugged is False
    assert power.conflict is True
    assert power.status == 'discharging'
    assert power.source == 'sysfs'


def test_charging_with_a_stale_offline_flag_is_plugged_in(tmp_path):
    """The mirror image: an offline mains file does not unplug a charging battery."""
    _mains(tmp_path, online=0)
    _battery(tmp_path, status='charging')

    power = PowerSupply.read_power(tmp_path)

    assert power.plugged is True
    assert power.conflict is True


def test_agreement_is_not_a_conflict(tmp_path):
    _mains(tmp_path, online=0)
    _battery(tmp_path, status='discharging')

    power = PowerSupply.read_power(tmp_path)

    assert power.plugged is False
    assert power.conflict is False


def test_not_charging_at_full_counts_as_plugged_in(tmp_path):
    """A full pack on mains reports 'not charging'; it is still plugged in."""
    _mains(tmp_path, online=1)
    _battery(tmp_path, status='Not charging')

    power = PowerSupply.read_power(tmp_path)

    assert power.plugged is True
    assert power.conflict is False


# -- machines that answer differently --------------------------------------

def test_a_desktop_with_only_mains_has_no_battery(tmp_path):
    _mains(tmp_path, online=1)

    power = PowerSupply.read_power(tmp_path)

    assert power.present is False
    assert power.plugged is True
    assert power.percent is None


def test_an_absent_battery_is_not_present(tmp_path):
    _mains(tmp_path, online=1)
    _battery(tmp_path, present=0)

    power = PowerSupply.read_power(tmp_path)

    assert power.present is False


def test_without_a_mains_device_the_battery_status_answers(tmp_path):
    _battery(tmp_path, status='discharging')

    power = PowerSupply.read_power(tmp_path)

    assert power.present is True
    assert power.plugged is False
    assert power.conflict is False


def test_an_unknown_status_leaves_plugged_unknown(tmp_path):
    _battery(tmp_path, status='unknown')

    power = PowerSupply.read_power(tmp_path)

    assert power.plugged is None
    assert power.present is True


def test_the_percentage_prefers_energy_over_capacity(tmp_path):
    _battery(tmp_path, energy_now=1234, energy_full=5000)
    (tmp_path / 'BAT0' / 'capacity').write_text('99\n', encoding='utf-8')

    power = PowerSupply.read_power(tmp_path)

    assert power.percent == pytest.approx(24.7)


def test_the_percentage_falls_back_to_capacity(tmp_path):
    folder = _device(tmp_path, 'BAT0', type='Battery', status='discharging',
                     capacity=42)
    assert folder.is_dir()

    power = PowerSupply.read_power(tmp_path)

    assert power.percent == pytest.approx(42.0)


def test_two_batteries_are_aggregated(tmp_path):
    _battery(tmp_path, name='BAT0', status='discharging',
             energy_now=1000, energy_full=2000)
    _battery(tmp_path, name='BAT1', status='discharging',
             energy_now=1000, energy_full=2000)

    power = PowerSupply.read_power(tmp_path)

    assert power.present is True
    assert power.percent == pytest.approx(50.0)
    assert len(power.supplies) == 2


# -- no sysfs at all -------------------------------------------------------

def test_unreadable_sysfs_falls_back_to_psutil(tmp_path, monkeypatch):
    monkeypatch.setattr(PowerSupply, 'psutil', types.SimpleNamespace(
        sensors_battery=lambda: types.SimpleNamespace(
            percent=55.55, power_plugged=False)))

    power = PowerSupply.read_power(tmp_path / 'missing')

    assert power.source == 'psutil'
    assert power.present is True
    assert power.plugged is False
    assert power.percent == pytest.approx(55.5)


def test_nothing_to_read_reports_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(PowerSupply, 'psutil', None)

    power = PowerSupply.read_power(tmp_path / 'missing')

    assert power.present is None
    assert power.plugged is None
    assert 'present=?' in PowerSupply.describe(power)


def test_describe_names_the_conflict_and_the_devices(tmp_path):
    _mains(tmp_path, online=1)
    _battery(tmp_path, status='discharging')

    text = PowerSupply.describe(PowerSupply.read_power(tmp_path))

    assert 'CONFLICT(mains-vs-battery)' in text
    assert 'BAT0(battery,discharging,73%)' in text


# -- the applet's own state machine ---------------------------------------

def _applet(selector='Settings', percent=100, present=None):
    tray = object.__new__(main_module.PwrTray)
    tray.battery = types.SimpleNamespace(present=present, plugged=True,
                                         percent=percent, selector=selector,
                                         synthetic=False)
    tray.rebuild_menu = False
    tray.get_params = lambda: types.SimpleNamespace(lo_battery_pct=20)
    return tray


def test_a_reading_on_battery_selects_the_matching_tier(monkeypatch):
    monkeypatch.setattr(main_module, 'prt', lambda *_a, **_k: None)
    tray = _applet()
    monkeypatch.setattr(tray, '_battery_reading', lambda: types.SimpleNamespace(
        present=True, plugged=False, percent=42.0))

    main_module.PwrTray.update_battery_status(tray)

    assert tray.battery.selector == 'HiBattery'
    assert tray.battery.plugged is False
    assert tray.battery.percent == pytest.approx(42.0)
    assert tray.rebuild_menu is True


def test_a_low_reading_selects_lobattery(monkeypatch):
    monkeypatch.setattr(main_module, 'prt', lambda *_a, **_k: None)
    tray = _applet()
    monkeypatch.setattr(tray, '_battery_reading', lambda: types.SimpleNamespace(
        present=True, plugged=False, percent=8.0))

    main_module.PwrTray.update_battery_status(tray)

    assert tray.battery.selector == 'LoBattery'


def test_a_plugged_reading_selects_settings(monkeypatch):
    monkeypatch.setattr(main_module, 'prt', lambda *_a, **_k: None)
    tray = _applet(selector='HiBattery', percent=77)
    monkeypatch.setattr(tray, '_battery_reading', lambda: types.SimpleNamespace(
        present=True, plugged=True, percent=100.0))

    main_module.PwrTray.update_battery_status(tray)

    assert tray.battery.selector == 'Settings'


def test_a_battery_that_appears_later_is_picked_up(monkeypatch):
    """``present is False`` used to be a permanent verdict for the process."""
    monkeypatch.setattr(main_module, 'prt', lambda *_a, **_k: None)
    tray = _applet(selector='HiBattery', percent=55, present=False)
    tray.battery.synthetic = True
    monkeypatch.setattr(tray, '_battery_reading', lambda: types.SimpleNamespace(
        present=True, plugged=False, percent=88.0))

    main_module.PwrTray.update_battery_status(tray)

    assert tray.battery.present is True
    assert tray.battery.synthetic is False
    assert tray.battery.percent == pytest.approx(88.0)


def test_losing_the_battery_keeps_the_cycler_state(monkeypatch):
    monkeypatch.setattr(main_module, 'prt', lambda *_a, **_k: None)
    tray = _applet(selector='HiBattery', percent=88, present=None)
    monkeypatch.setattr(tray, '_battery_reading', lambda: None)

    main_module.PwrTray.update_battery_status(tray)

    assert tray.battery.present is False
    assert tray.battery.selector == 'HiBattery'
    assert tray.battery.percent == 88
    assert tray.rebuild_menu is True
