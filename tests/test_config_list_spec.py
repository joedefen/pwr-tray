"""Regression tests for list-valued settings.

A malformed `*_list` value must fall back to the configured default *list*.
The bug was a raw-string test (`if not value`) that both reported the wrong
case in the log and stored the parse result -- which is None on failure.  The
defaults are held as the literal config text so they can be written with
configparser, so a skipped key used to leave that string (``'[15, 30]'``)
sitting in ``params`` instead of a list; downstream ``min_list[0] * 60`` then
multiplied a character.  Every list key must therefore hold a list of positive
ints after ``update_config()``.
"""

from pwr_tray.IniTool import IniTool, to_array

DEFAULT_SETTINGS_LOCK = [15, 30]
DEFAULT_SETTINGS_SLEEP = [5, 30]


def write_config(home, text):
    """Write `text` as ~/.config/pwr-tray/config.ini under the isolated HOME."""
    folder = home / '.config' / 'pwr-tray'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'config.ini').write_text(text, encoding='utf-8')


def load_tool(home, text):
    """Return the IniTool after parsing `text`, with its params reloaded."""
    write_config(home, text)
    ini_tool = IniTool()
    assert ini_tool.update_config() is True
    return ini_tool


def assert_all_lists(ini_tool):
    """Every selector's list settings are lists of at least two positive ints."""
    for selector, params in ini_tool.params_by_selector.items():
        for key in ('lock_min_list', 'sleep_min_list'):
            value = getattr(params, key)
            assert isinstance(value, list), f'{selector}.{key} is {value!r}'
            assert len(value) >= 2, f'{selector}.{key} is {value!r}'
            assert all(isinstance(val, int) and val > 0 for val in value), \
                f'{selector}.{key} is {value!r}'


def test_malformed_settings_list_uses_default(isolated_home):
    """`not-json` yields the default list, not None and not the raw string."""
    ini_tool = load_tool(isolated_home, """\
[Settings]
lock_min_list = not-json
""")

    settings = ini_tool.params_by_selector['Settings']
    assert settings.lock_min_list == DEFAULT_SETTINGS_LOCK
    assert ini_tool.get_current_vals('Settings', 'lock_min_list') == \
        DEFAULT_SETTINGS_LOCK
    assert_all_lists(ini_tool)


def test_empty_settings_list_uses_default(isolated_home):
    """An empty value is malformed too: it must not become '' or None."""
    ini_tool = load_tool(isolated_home, """\
[Settings]
sleep_min_list =
""")

    settings = ini_tool.params_by_selector['Settings']
    assert settings.sleep_min_list == DEFAULT_SETTINGS_SLEEP
    assert_all_lists(ini_tool)


def test_non_list_json_uses_default(isolated_home):
    """Valid JSON that is neither an int nor a list is a bad list spec."""
    ini_tool = load_tool(isolated_home, """\
[Settings]
lock_min_list = {"mins": 5}
sleep_min_list = "soon"
""")

    settings = ini_tool.params_by_selector['Settings']
    assert settings.lock_min_list == DEFAULT_SETTINGS_LOCK
    assert settings.sleep_min_list == DEFAULT_SETTINGS_SLEEP
    assert_all_lists(ini_tool)


def test_malformed_tier_list_inherits_settings(isolated_home):
    """A bad tier spec keeps the parsed [Settings] list, not a tier default."""
    ini_tool = load_tool(isolated_home, """\
[Settings]
lock_min_list = [3, 7]

[HiBattery]
lock_min_list = while
""")

    hi_battery = ini_tool.params_by_selector['HiBattery']
    assert hi_battery.lock_min_list == [3, 7]
    assert_all_lists(ini_tool)


def test_missing_list_key_uses_default(isolated_home):
    """A list key absent from the section cannot stay as the config text."""
    ini_tool = load_tool(isolated_home, """\
[Settings]
gui_editor = my-edit
""")

    settings = ini_tool.params_by_selector['Settings']
    assert settings.lock_min_list == DEFAULT_SETTINGS_LOCK
    assert settings.sleep_min_list == DEFAULT_SETTINGS_SLEEP
    assert settings.gui_editor == 'my-edit'
    assert_all_lists(ini_tool)


def test_invalid_list_elements_are_dropped(isolated_home):
    """Zero/negative/non-int entries never leak into a timeout list."""
    ini_tool = load_tool(isolated_home, """\
[Settings]
lock_min_list = [0, 40]
sleep_min_list = [-5, 12, "x", true]
""")

    settings = ini_tool.params_by_selector['Settings']
    # The single usable value is duplicated ("always want two"), never 0.
    assert settings.lock_min_list == [40, 40]
    assert settings.sleep_min_list == [12, 12]
    assert_all_lists(ini_tool)


def test_single_int_spec_is_doubled(isolated_home):
    """A bare integer spec means "the same timeout twice"."""
    ini_tool = load_tool(isolated_home, """\
[Settings]
lock_min_list = 20
""")

    settings = ini_tool.params_by_selector['Settings']
    assert settings.lock_min_list == [20, 20]
    assert_all_lists(ini_tool)


def test_to_array_contract():
    """Direct unit checks for the parser used by the fallbacks above."""
    assert to_array('[1,2]') == [1, 2]
    assert to_array('20') == [20, 20]
    assert to_array('[7]') == [7, 7]
    assert to_array('[0, 40]') == [40, 40]
    assert to_array('[1,2,3]') == [1, 2, 3]
    assert to_array('') is None
    assert to_array('not-json') is None
    assert to_array('[]') is None
    assert to_array('[0]') is None
    assert to_array('"5"') is None
    assert to_array('{"a": 1}') is None
