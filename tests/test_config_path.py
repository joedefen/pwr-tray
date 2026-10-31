"""Regression tests for config-path robustness.

`update_config()` used to call `os.path.getmtime()` unguarded, and it is called
from `on_timeout()` inside a Qt slot.  Deleting `config.ini` -- or leaving a
malformed one behind -- therefore produced one traceback per tick instead of a
single recoverable warning.  These tests pin the replacement contract:

- no exception escapes, whatever happens to the path;
- the last good values stay in effect while the file is unusable;
- the problem is reported once per change, not once per tick;
- a config that is never readable still yields the built-in defaults;
- the file is picked up again when it reappears.
"""

import os

import pwr_tray.IniTool as ini_module
from pwr_tray.IniTool import IniTool


def config_path(home):
    """Path of ~/.config/pwr-tray/config.ini under the isolated HOME."""
    return home / '.config' / 'pwr-tray' / 'config.ini'


def write_config(home, text, mtime=None):
    """Write `text` as config.ini, optionally forcing a distinct mtime."""
    path = config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def loaded_tool(home):
    """An IniTool that has successfully parsed a two-value config."""
    write_config(home, """\
[Settings]
lock_min_list = [3, 7]
sleep_min_list = [9, 11]
gui_editor = my-edit

[HiBattery]
lock_min_list = [4, 5]
""")
    ini_tool = IniTool()
    assert ini_tool.update_config() is True
    return ini_tool


def warnings(monkeypatch):
    """Capture prt() output and return the list the messages land in."""
    messages = []
    monkeypatch.setattr(ini_module, 'prt',
                        lambda *args, **kwargs: messages.append(' '.join(
                            str(arg) for arg in args)))
    return messages


# --------------------------------------------------------------------------
# deleted config.ini
# --------------------------------------------------------------------------

def test_deleted_config_does_not_raise_and_keeps_values(isolated_home):
    ini_tool = loaded_tool(isolated_home)
    before = ini_tool.params_by_selector['Settings'].lock_min_list

    config_path(isolated_home).unlink()

    # The tick loop must see a plain "no change", not a FileNotFoundError.
    assert ini_tool.update_config() is False
    assert ini_tool.params_by_selector['Settings'].lock_min_list == before


def test_deleted_config_is_reported_once_per_absence(isolated_home, monkeypatch):
    ini_tool = loaded_tool(isolated_home)
    config_path(isolated_home).unlink()
    messages = warnings(monkeypatch)

    for _ in range(10):          # ten poll ticks
        ini_tool.update_config()

    reported = [line for line in messages if 'WARN' in line]
    assert len(reported) == 1
    assert 'last good values' in reported[0]
    assert 'config.ini' in reported[0]


def test_recreated_config_is_picked_up_again(isolated_home):
    ini_tool = loaded_tool(isolated_home)
    path = config_path(isolated_home)
    missing_at = os.path.getmtime(path)
    path.unlink()
    assert ini_tool.update_config() is False

    write_config(isolated_home, """\
[Settings]
lock_min_list = [6, 8]
""", mtime=missing_at + 10)
    assert ini_tool.update_config() is True
    assert ini_tool.params_by_selector['Settings'].lock_min_list == [6, 8]


# --------------------------------------------------------------------------
# unparsable config.ini
# --------------------------------------------------------------------------

def test_malformed_config_keeps_last_good_values(isolated_home):
    ini_tool = loaded_tool(isolated_home)
    old_mtime = os.path.getmtime(config_path(isolated_home))

    write_config(isolated_home, '[Settings\nlock_min_list = [1, 2]\n',
                 mtime=old_mtime + 10)

    assert ini_tool.update_config() is False
    settings = ini_tool.params_by_selector['Settings']
    assert settings.lock_min_list == [3, 7]
    assert settings.gui_editor == 'my-edit'


def test_malformed_config_is_reported_once_per_save(isolated_home, monkeypatch):
    ini_tool = loaded_tool(isolated_home)
    old_mtime = os.path.getmtime(config_path(isolated_home))
    write_config(isolated_home, '[Settings\nlock_min_list = [1, 2]\n',
                 mtime=old_mtime + 10)
    messages = warnings(monkeypatch)

    for _ in range(10):
        ini_tool.update_config()

    assert len([line for line in messages if 'WARN' in line]) == 1

    # Saving the broken file again is a new event and warns again.
    write_config(isolated_home, '[Settings\n', mtime=old_mtime + 20)
    ini_tool.update_config()
    assert len([line for line in messages if 'WARN' in line]) == 2


# --------------------------------------------------------------------------
# never readable / not creatable
# --------------------------------------------------------------------------

def test_unreadable_config_falls_back_to_defaults(isolated_home):
    """No config file has ever been read: callers must still get params."""
    ini_tool = IniTool(paths_only=True)      # skips ensure_ini_file()

    assert ini_tool.update_config() is True
    settings = ini_tool.params_by_selector['Settings']
    assert settings.lock_min_list == [15, 30]
    assert settings.sleep_min_list == [5, 30]
    assert ini_tool.params_by_selector['HiBattery'].lock_min_list == [10, 20]
    assert ini_tool.params_by_selector['LoBattery'].sleep_min_list == [1, 1]

    # And it settles: the second tick is not a change.
    assert ini_tool.update_config() is False


def test_unwritable_config_folder_does_not_abort_startup(isolated_home,
                                                          monkeypatch):
    def deny(*args, **kwargs):
        raise PermissionError(13, 'Permission denied')

    monkeypatch.setattr(ini_module.os, 'makedirs', deny)

    ini_tool = IniTool()                     # must not raise
    assert ini_tool.update_config() is True
    assert ini_tool.params_by_selector['Settings'].power_down is False
