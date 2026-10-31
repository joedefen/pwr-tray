"""Regression tests for PwrTray.restore_picks() and save_picks().

Sleep timeouts were once restored through the *lock* helper, a partial
picks.json aborted the whole restore, and the failure path reported success.
The suite runs headless via the stubs in the root
conftest.py and never touches the real ~/.config/pwr-tray.
"""

import json

import pytest

from pwr_tray.IniTool import IniTool
from pwr_tray.main import PwrTray

SELECTORS = ['LoBattery', 'HiBattery', 'Settings']

# Defaults from IniTool, used to prove that config values survive a bad restore.
DEFAULT_SETTINGS = ([15, 30], [5, 30])
DEFAULT_HI_BATTERY = ([10, 20], [1, 10])


def new_tray():
    """A PwrTray with only the state restore_picks()/save_picks() need."""
    ini_tool = IniTool()
    ini_tool.update_config()
    tray = object.__new__(PwrTray)
    tray.ini_tool = ini_tool
    tray.picks_file = ini_tool.picks_path
    tray.mode = 'SleepAfterLock'
    tray.enable_playerctl = True
    tray.quick = False
    tray.poll_100ms = False
    return tray


@pytest.fixture
def tray(isolated_home):
    tray = new_tray()
    PwrTray.singleton = tray
    try:
        yield tray
    finally:
        PwrTray.singleton = None


def write_picks(tray, payload):
    """payload is either a dict serialised as JSON, or a raw string."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    with open(tray.picks_file, 'w', encoding='utf-8') as handle:
        handle.write(text + '\n')


def pick_lists(tray, selector):
    return (tray.get_lock_min_list(selector), tray.get_sleep_min_list(selector))


def test_lock_and_sleep_are_restored_on_their_own_axes(tray):
    write_picks(tray, {
        'mode': 'Presentation',
        'enable_playerctl': False,
        'lock_mins': {'Settings': 30, 'HiBattery': 20, 'LoBattery': 1},
        'sleep_mins': {'Settings': 30, 'HiBattery': 10, 'LoBattery': 1},
    })

    assert tray.restore_picks() is True

    assert tray.mode == 'Presentation'
    assert tray.enable_playerctl is False
    # Each axis rotates its own list.  Restoring sleep via the lock helper would
    # leave Settings sleep at [5, 30] and rotate HiBattery lock off 20 instead.
    assert pick_lists(tray, 'Settings') == ([30, 15], [30, 5])
    assert pick_lists(tray, 'HiBattery') == ([20, 10], [10, 1])


def test_lowercase_and_missing_optional_keys_fall_back_to_current_state(tray):
    write_picks(tray, {'lock_mins': {'Settings': 30, 'HiBattery': 20, 'LoBattery': 1},
                       'sleep_mins': {'Settings': 30, 'HiBattery': 10, 'LoBattery': 1}})

    assert tray.restore_picks() is True

    assert tray.mode == 'SleepAfterLock'
    assert tray.enable_playerctl is True
    assert pick_lists(tray, 'Settings') == ([30, 15], [30, 5])


def test_missing_sleep_axis_still_restores_lock(tray):
    """An older / hand-edited picks.json must not abort the whole restore."""
    write_picks(tray, {'mode': 'LockOnly',
                       'lock_mins': {'Settings': 30, 'HiBattery': 20, 'LoBattery': 1}})

    assert tray.restore_picks() is False

    assert tray.mode == 'LockOnly'
    assert pick_lists(tray, 'Settings') == ([30, 15], [5, 30])
    assert pick_lists(tray, 'HiBattery') == ([20, 10], [1, 10])


def test_missing_selector_only_skips_that_selector(tray):
    write_picks(tray, {'lock_mins': {'Settings': 30},
                       'sleep_mins': {'Settings': 30}})

    assert tray.restore_picks() is False

    assert pick_lists(tray, 'Settings') == ([30, 15], [30, 5])
    assert pick_lists(tray, 'HiBattery') == DEFAULT_HI_BATTERY


@pytest.mark.parametrize('bad_value', ['nope', True, None, 30.5, [30]])
def test_non_int_timeout_is_skipped(tray, bad_value):
    write_picks(tray, {'lock_mins': {'Settings': 30, 'HiBattery': bad_value},
                       'sleep_mins': {'Settings': 30, 'HiBattery': 10}})

    assert tray.restore_picks() is False

    assert pick_lists(tray, 'Settings') == ([30, 15], [30, 5])
    # HiBattery's bad lock value is ignored, but its valid sleep value applies.
    assert pick_lists(tray, 'HiBattery') == ([10, 20], [10, 1])


@pytest.mark.parametrize('bad_axis', [None, [], 'x', 7])
def test_axis_that_is_not_an_object_is_ignored(tray, bad_axis):
    write_picks(tray, {'lock_mins': bad_axis,
                       'sleep_mins': {'Settings': 30}})

    assert tray.restore_picks() is False

    # The unusable lock axis leaves lock_mins alone; the good sleep axis applies.
    assert pick_lists(tray, 'Settings') == ([15, 30], [30, 5])


def test_invalid_scalars_are_reported_not_applied(tray):
    write_picks(tray, {'mode': 7, 'enable_playerctl': 'yes'})

    assert tray.restore_picks() is False

    assert tray.mode == 'SleepAfterLock'
    assert tray.enable_playerctl is True


@pytest.mark.parametrize('raw', ['[1, 2, 3]', '"picks"', 'null', '{not json', ''])
def test_unusable_picks_file_keeps_configured_values(tray, raw):
    write_picks(tray, raw)

    assert tray.restore_picks() is False

    assert tray.mode == 'SleepAfterLock'
    assert pick_lists(tray, 'Settings') == DEFAULT_SETTINGS


def test_missing_picks_file_keeps_configured_values(tray):
    assert tray.restore_picks() is False

    assert tray.mode == 'SleepAfterLock'
    assert pick_lists(tray, 'Settings') == DEFAULT_SETTINGS
    assert pick_lists(tray, 'HiBattery') == DEFAULT_HI_BATTERY


def test_save_then_restore_round_trip(tray):
    tray.mode = 'LockOnly'
    tray.enable_playerctl = False
    tray.get_lock_rotated_list('Settings', first=30)
    tray.get_sleep_rotated_list('Settings', first=30)

    tray.save_picks()

    with open(tray.picks_file, encoding='utf-8') as handle:
        saved = json.load(handle)
    assert saved['lock_mins']['Settings'] == 30
    assert saved['sleep_mins']['Settings'] == 30

    reloaded = new_tray()
    assert reloaded.restore_picks() is True
    assert reloaded.mode == 'LockOnly'
    assert reloaded.enable_playerctl is False
    assert pick_lists(reloaded, 'Settings') == ([30, 15], [30, 5])
