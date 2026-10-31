"""Regression tests for config.ini inheritance.

README guarantees that missing or invalid settings in [HiBattery] and
[LoBattery] are inherited from [Settings].  The parsed [Settings] values must
therefore become the base for both tiers, not the hard-coded defaults.

The other half of the rule: "inherit from [Settings]" means the values the user
*wrote* there.  A key absent from both the file's [Settings] and the tier has
to use the tier's own entry in ``IniTool.defaults``; before that rule the table
was documentation nothing honoured.
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


def test_tier_defaults_win_when_settings_does_not_set_the_key(isolated_home):
    """R3: the per-tier table, not the [Settings] default, decides."""
    params = load_params(isolated_home, """\
[Settings]
gui_editor = my-edit
""")

    # [Settings] itself keeps the built-in defaults.
    settings = params['Settings']
    assert settings.power_down is False
    assert settings.lock_min_list == [15, 30]
    assert settings.sleep_min_list == [5, 30]

    # LoBattery is absent, and power_down is absent from [Settings]: the
    # LoBattery table says True, so it is True.
    lo_battery = params['LoBattery']
    assert lo_battery.power_down is True
    assert lo_battery.lock_min_list == [1, 1]
    assert lo_battery.sleep_min_list == [1, 1]
    assert lo_battery.gui_editor == 'my-edit'   # still inherited

    hi_battery = params['HiBattery']
    assert hi_battery.power_down is False
    assert hi_battery.lock_min_list == [10, 20]
    assert hi_battery.sleep_min_list == [1, 10]


def test_explicit_settings_value_beats_the_tier_default(isolated_home):
    """R3 must not reintroduce v1 #4: a written [Settings] value wins."""
    params = load_params(isolated_home, """\
[Settings]
power_down = False
lock_min_list = [3, 7]
""")

    lo_battery = params['LoBattery']
    assert lo_battery.power_down is False
    assert lo_battery.lock_min_list == [3, 7]
    # The keys [Settings] did not set still come from the LoBattery table.
    assert lo_battery.sleep_min_list == [1, 1]


def test_invalid_settings_value_does_not_block_the_tier_default(isolated_home):
    """An unusable [Settings] value counts as absent, so the tier default applies."""
    params = load_params(isolated_home, """\
[Settings]
power_down = maybe
""")

    assert params['Settings'].power_down is False
    assert params['LoBattery'].power_down is True


def test_empty_tier_section_gets_the_tier_defaults(isolated_home):
    params = load_params(isolated_home, """\
[Settings]

[LoBattery]
sleep_min_list = [7, 9]
""")

    lo_battery = params['LoBattery']
    assert lo_battery.power_down is True
    assert lo_battery.lock_min_list == [1, 1]
    assert lo_battery.sleep_min_list == [7, 9]


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
