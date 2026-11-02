"""The ``[ServiceAllowed]`` section: read it, and edit it without ruining it.

The section is the whole policy of *which desktops may run the service*, and it
lives in ``config.ini`` for one reason: the user is meant to be able to open it
in an editor.  So the reader is forgiving (missing rows, comments, ``yes``/``no``)
and the writer is surgical -- ``configparser.write()`` would reorder the file and
delete every comment the user wrote, which is exactly what must not happen.
"""

from pathlib import Path

from pwr_tray import service
from pwr_tray.IniTool import (
    SERVICE_SECTION,
    default_config_text,
    parse_service_allowed,
    set_flag_text,
)

TEXT = """\
[Settings]
debug_mode = True
# a comment that has to survive

[ServiceAllowed]
# which desktops may run it
i3-x11 = True
niri-wayland = False  # desktop at home
kde-x11: no
"""


def test_parse_reads_the_section_in_file_order():
    allowed = parse_service_allowed(TEXT)
    assert list(allowed) == ['i3-x11', 'niri-wayland', 'kde-x11']
    assert allowed == {'i3-x11': True, 'niri-wayland': False, 'kde-x11': False}


def test_parse_accepts_the_words_a_hand_edited_file_uses():
    text = ('[ServiceAllowed]\na = true\nb = YES\nc = on\nd = 1\n'
            'e = false\nf = NO\ng = off\nh = 0\ni = maybe\n')
    allowed = parse_service_allowed(text)
    assert [name for name, on in allowed.items() if on] == ['a', 'b', 'c', 'd']
    assert not any(allowed[name] for name in 'efghi')


def test_parse_of_a_missing_section_allows_nothing():
    assert parse_service_allowed('[Settings]\ndebug_mode = True\n') == {}
    assert parse_service_allowed('') == {}


def test_a_commented_out_row_is_not_a_row():
    text = '[ServiceAllowed]\n#i3-x11 = True\n;i3-x11 = True\n'
    assert parse_service_allowed(text) == {}


def test_set_flag_keeps_the_inline_comment_and_the_rest_of_the_file():
    updated = set_flag_text(TEXT, 'niri-wayland', True)
    assert 'niri-wayland = True  # desktop at home' in updated
    assert '# a comment that has to survive' in updated
    assert '[Settings]\ndebug_mode = True' in updated
    assert parse_service_allowed(updated)['i3-x11'] is True


def test_set_flag_handles_the_colon_separator():
    updated = set_flag_text(TEXT, 'kde-x11', True)
    assert 'kde-x11: True' in updated


def test_set_flag_appends_a_new_row_inside_the_section():
    updated = set_flag_text(TEXT, 'sway-wayland', True)
    lines = updated.splitlines()
    section = lines.index('[ServiceAllowed]')
    new = lines.index('sway-wayland = True')
    # Before the end of the file and after the section header: no new section,
    # nothing appended past the trailing rows.
    assert new > section
    assert new == len(lines) - 1
    assert parse_service_allowed(updated)['sway-wayland'] is True


def test_a_missing_section_is_created_with_the_full_list():
    keys = ['i3-x11', 'niri-wayland', 'kde-x11']
    updated = set_flag_text('[Settings]\ndebug_mode = True\n', 'niri-wayland',
                            True, keys=keys)
    allowed = parse_service_allowed(updated)
    assert list(allowed) == keys
    assert allowed == {'i3-x11': False, 'niri-wayland': True, 'kde-x11': False}
    assert SERVICE_SECTION in updated
    assert '--svc-install' in updated          # the header explains itself


def test_an_empty_file_still_gets_a_usable_section():
    updated = set_flag_text('', 'i3-x11', True, keys=['i3-x11'])
    assert parse_service_allowed(updated) == {'i3-x11': True}
    assert updated.endswith('\n')


def test_the_section_name_is_matched_case_insensitively():
    updated = set_flag_text('[serviceallowed]\ni3-x11 = False\n', 'i3-x11', True)
    assert 'i3-x11 = True' in updated


def test_default_config_text_is_a_complete_config():
    text = default_config_text()
    assert '[Settings]' in text
    assert 'debug_mode' in text
    assert parse_service_allowed(text) == {}


def test_execute_writes_the_edited_config_in_one_step(tmp_path):
    """The edit goes through the ordinary write step, and leaves no temp file."""
    folder = tmp_path / 'cfg'
    folder.mkdir()
    config = folder / 'config.ini'
    plan = service.Plan(steps=[service.Step(
        'record', write=(config, set_flag_text(TEXT, 'niri-wayland', True)))])

    service.execute(plan, run=lambda argv: (0, ''))

    assert parse_service_allowed(
        config.read_text(encoding='utf-8'))['niri-wayland'] is True
    assert sorted(path.name for path in folder.iterdir()) == ['config.ini']


def test_plan_flag_is_a_no_op_when_the_file_already_says_so(tmp_path):
    config = tmp_path / '.config' / 'pwr-tray' / 'config.ini'
    config.parent.mkdir(parents=True)
    config.write_text('[ServiceAllowed]\ni3-x11 = True\n', encoding='utf-8')

    env = {'HOME': str(tmp_path)}
    assert service.plan_flag('i3-x11', True, env) is None
    assert service.plan_flag('niri-wayland', False, env) is None
    assert service.plan_flag('niri-wayland', True, env) is not None


def test_plan_flag_does_not_create_a_section_to_write_false(tmp_path):
    """`--svc-uninstall` before any install must not invent a policy file."""
    env = {'HOME': str(tmp_path)}
    assert service.plan_flag('i3-x11', False, env) is None
    assert not (tmp_path / '.config' / 'pwr-tray' / 'config.ini').exists()


def test_allowed_desktops_of_a_missing_file_is_empty(tmp_path):
    assert service.allowed_desktops({'HOME': str(tmp_path)}) == {}
    assert service.desktop_allowed('i3-x11', {'HOME': str(tmp_path)}) is False


def test_allowed_desktops_reads_the_applet_config(tmp_path):
    config = Path(tmp_path) / '.config' / 'pwr-tray' / 'config.ini'
    config.parent.mkdir(parents=True)
    config.write_text('[ServiceAllowed]\nniri-wayland = True\n', encoding='utf-8')

    env = {'HOME': str(tmp_path)}
    assert service.allowed_desktops(env) == {'niri-wayland': True}
    assert service.desktop_allowed('niri-wayland', env) is True
    assert service.desktop_allowed('kde-x11', env) is False
