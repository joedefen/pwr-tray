"""Regression tests for config.ini inheritance.

README guarantees that missing or invalid settings in [HiBattery] and
[LoBattery] are inherited from [Settings].  The parsed [Settings] values must
therefore become the base for both tiers, not the hard-coded defaults.
"""

from pwr_tray.IniTool import IniTool


def write_config(home, text):
    """Write `text` as ~/.config/pwr-tray/config.ini under the isolated HOME."""
    folder = home / '.config' / 'pwr-tray'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'config.ini').write_text(text, encoding='utf-8')


def load_params(home, text):
    """Return the parsed params_by_selector after reading `text`."""
    write_config(home, text)
    ini_tool = IniTool()
    assert ini_tool.update_config() is True
    return ini_tool.params_by_selector


def test_tiers_inherit_user_settings_values(isolated_home):
    params = load_params(isolated_home, """\
[Settings]
lock_min_list = [3, 7]
sleep_min_list = [9, 11]
gui_editor = my-edit

[HiBattery]
lock_min_list = [4, 5]
""")

    settings = params['Settings']
    assert settings.lock_min_list == [3, 7]
    assert settings.sleep_min_list == [9, 11]
    assert settings.gui_editor == 'my-edit'

    # HiBattery overrides only its lock list; everything else comes from Settings.
    hi_battery = params['HiBattery']
    assert hi_battery.lock_min_list == [4, 5]
    assert hi_battery.sleep_min_list == [9, 11]
    assert hi_battery.gui_editor == 'my-edit'
    assert hi_battery.lo_battery_pct == settings.lo_battery_pct

    # LoBattery is absent, so it inherits every Settings value -- not
    # HiBattery's overridden lock list.
    lo_battery = params['LoBattery']
    assert lo_battery.lock_min_list == [3, 7]
    assert lo_battery.sleep_min_list == [9, 11]
    assert lo_battery.gui_editor == 'my-edit'


def test_invalid_tier_values_inherit_settings(isolated_home):
    params = load_params(isolated_home, """\
[Settings]
power_down = True
lock_min_list = [3, 7]
sleep_min_list = [9, 11]

[HiBattery]
power_down = maybe
lock_min_list = not-json
sleep_min_list = [4, 6]
""")

    hi_battery = params['HiBattery']
    # Invalid values fall back to the Settings value, not a tier default.
    assert hi_battery.power_down is True
    assert hi_battery.lock_min_list == [3, 7]
    # A valid tier value still wins.
    assert hi_battery.sleep_min_list == [4, 6]
