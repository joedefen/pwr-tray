"""The one-time config normaliser: add what is missing, never remove anything.

``config.ini`` is hand-editable, so a normaliser is only acceptable if it is
*additive* and idempotent: a file that already holds every expected row must
come back byte-for-byte unchanged (and must not even be opened for writing),
while an older file gains the new keys without losing a comment, a blank line,
its ordering, or the ``key: value`` separator someone may have used.

The two exceptions are deliberate and pinned here:

* a row the user *commented out* counts as present -- re-adding it live would
  undo a decision taken by hand;
* ``[HiBattery]``/``[LoBattery]`` rows are never filled in, because a written
  tier key overrides ``[Settings]`` while an absent one inherits it.
"""

from pathlib import Path

from pwr_tray.IniTool import (
    SERVICE_SECTION,
    IniTool,
    default_config_text,
    normalize_config_text,
    service_rows_not_supported,
)


def test_a_missing_settings_key_is_added_and_the_rest_is_untouched():
    text = '[Settings]\n# a comment that has to survive\ndebug_mode = True\n'

    updated, changes = normalize_config_text(text, IniTool.defaults, [])

    assert 'debug_mode = True' in updated
    assert '# a comment that has to survive' in updated
    assert 'confirm_session_end = False' in updated
    assert changes


def test_a_commented_out_row_is_not_re_added_live():
    """`#key = value` is how a hand editor disables a setting on purpose."""
    text = '[Settings]\n#turn_off_monitors = True\n'

    updated, changes = normalize_config_text(text, IniTool.defaults, [])

    assert '#turn_off_monitors = True' in updated
    assert 'turn_off_monitors = False' not in updated
    assert 'turn_off_monitors' not in ' '.join(changes)


def test_battery_tier_rows_are_never_materialised():
    """A tier key that is present overrides [Settings]; absent, it inherits."""
    text = '[Settings]\npower_down = False\n'

    updated, _changes = normalize_config_text(text, IniTool.defaults, [])

    assert '[HiBattery]' not in updated
    assert '[LoBattery]' not in updated


def test_a_complete_file_is_returned_unchanged_and_reports_no_changes():
    text = default_config_text()

    updated, changes = normalize_config_text(text, IniTool.defaults, [])

    assert updated == text
    assert changes == []


def test_normalising_twice_is_a_no_op():
    text = '[Settings]\ndebug_mode = True\n'
    once, first_changes = normalize_config_text(text, IniTool.defaults,
                                                ['i3-x11', 'niri-wayland'])
    assert first_changes

    twice, second_changes = normalize_config_text(once, IniTool.defaults,
                                                  ['i3-x11', 'niri-wayland'])

    assert twice == once
    assert second_changes == []


def test_a_missing_settings_section_is_created():
    updated, changes = normalize_config_text('[HiBattery]\n', IniTool.defaults, [])

    assert '[Settings]' in updated
    assert 'confirm_session_end = False' in updated
    assert any('[Settings]' in change for change in changes)


def test_service_allowed_is_created_with_every_desktop_false():
    updated, changes = normalize_config_text('[Settings]\n', IniTool.defaults,
                                             ['i3-x11', 'niri-wayland'])

    assert f'[{SERVICE_SECTION}]' in updated
    # Consent is never inferred: a new row is False, which is what a missing
    # row already meant.
    assert 'i3-x11 = False' in updated
    assert 'niri-wayland = False' in updated
    assert '= True' not in updated
    # The section explains itself, exactly as --svc-install would create it.
    assert '# Which desktops may run pwr-tray' in updated
    assert any(SERVICE_SECTION in change for change in changes)


def test_a_new_desktop_is_appended_inside_the_existing_section():
    text = ('[Settings]\n\n'
            '[ServiceAllowed]\n'
            '# which desktops may run it\n'
            'i3-x11 = True\n')

    updated, _changes = normalize_config_text(text, IniTool.defaults,
                                             ['i3-x11', 'sway-wayland'])

    assert 'i3-x11 = True' in updated          # the user's answer is untouched
    assert 'sway-wayland = False' in updated
    assert updated.index('sway-wayland') > updated.index(f'[{SERVICE_SECTION}]')


def test_a_row_for_a_dropped_desktop_is_reported_and_kept():
    text = '[ServiceAllowed]\ni3-x11 = True\nold-de-x11 = True\n'

    assert service_rows_not_supported(text, ['i3-x11']) == ['old-de-x11']

    updated, _changes = normalize_config_text(text, IniTool.defaults, ['i3-x11'])
    assert 'old-de-x11 = True' in updated


def test_an_unknown_key_is_kept():
    text = '[Settings]\nmystery = 1\n'

    updated, _changes = normalize_config_text(text, IniTool.defaults, [])

    assert 'mystery = 1' in updated


def test_an_empty_file_is_left_empty():
    """`ensure_ini_file()` writes the defaults; an emptied file is a choice."""
    assert normalize_config_text('', IniTool.defaults, ['i3-x11']) == ('', [])


# -- the write path --------------------------------------------------------

def test_normalize_config_writes_once_then_leaves_the_file_alone(isolated_home):
    tool = IniTool(paths_only=False)
    config = Path(tool.ini_path)
    config.write_text('[Settings]\ndebug_mode = True\n', encoding='utf-8')

    assert tool.normalize_config(known_desktops=['niri-wayland'])
    after_first = config.read_text(encoding='utf-8')
    assert 'debug_mode = True' in after_first
    assert 'confirm_session_end = False' in after_first
    assert 'niri-wayland = False' in after_first

    # Idempotent: nothing to add, so the file is not rewritten at all.
    assert tool.normalize_config(known_desktops=['niri-wayland']) == []
    assert config.read_text(encoding='utf-8') == after_first


def test_a_fresh_config_gets_the_new_key_from_the_defaults(isolated_home):
    tool = IniTool(paths_only=False)

    assert 'confirm_session_end = False' in Path(tool.ini_path).read_text(
        encoding='utf-8')
    assert tool.normalize_config(known_desktops=[]) == []


def test_a_missing_config_is_not_an_error(isolated_home):
    tool = IniTool(paths_only=True)

    assert tool.normalize_config(known_desktops=['i3-x11']) == []
