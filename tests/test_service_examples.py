"""Validate the one systemd --user unit shipped in pwr_tray/resources/systemd/.

The unit is not documentation: it lives inside the package, so the wheel
contains it, `pwr-tray --svc-install` copies it to
``~/.config/systemd/user/``, and systemd is what actually runs it on a user's
machine.  It is also a **constant** -- it names no desktop, pins no display
variable and has nothing to substitute -- which is what lets one file serve
every desktop on a machine while ``[ServiceAllowed]`` in ``config.ini`` decides
which of them may run it.

The checks here are the reason both promises are safe: the unit must parse,
carry the directives systemd needs, stay free of the session snapshot that used
to make it wrong after a desktop switch, and keep the one restart policy that
lets a logout finish.
"""

import re
from pathlib import Path

import pytest

from pwr_tray import service

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = sorted((ROOT / 'pwr_tray' / 'resources' / 'systemd').glob('*.service'))
COMMANDS_YAML = ROOT / 'pwr_tray' / 'resources' / 'commands.yaml'
MAIN = ROOT / 'pwr_tray' / 'main.py'

# The two exits that mean "this will fail the same way forever": 3 when
# another instance holds the single-instance lock, 2 when the applet could
# not be constructed at all.
PERMANENT_EXIT_STATUSES = {'2', '3'}

#: Signals a session end is delivered with.  systemd already treats them as a
#: clean stop for `Restart=on-failure`; the unit names them so an edit to
#: `Restart=` cannot turn a logout back into a respawn.
STOP_SIGNALS = {'SIGTERM', 'SIGHUP', 'SIGINT', 'SIGPIPE'}


def parse_text(text):
    """Return {section: [(key, value), ...]} for systemd unit text.

    ``configparser`` cannot be used directly: unit files repeat keys such as
    ``After=`` and comments are allowed at any indentation.
    """
    sections = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in '#;':
            continue
        if line.startswith('[') and line.endswith(']'):
            current = line[1:-1]
            sections.setdefault(current, [])
            continue
        if current is not None and '=' in line:
            key, value = line.split('=', 1)
            sections[current].append((key.strip(), value.strip()))
    return sections


def parse_unit(path):
    return parse_text(path.read_text(encoding='utf-8'))


def values(sections, section, key):
    return [value for found, value in sections.get(section, []) if found == key]


def seconds(value):
    """Parse a plain systemd time span such as ``60``, ``60s`` or ``1min``."""
    match = re.fullmatch(
        r'(\d+)(s|sec|second|seconds|m|min|minute|minutes)?', value.strip())
    assert match, value
    unit = match.group(2) or 's'
    return int(match.group(1)) * (60 if unit.startswith('m') else 1)


def desktop_keys():
    """Return the keys under ``desktops:`` in commands.yaml, without PyYAML."""
    keys = set()
    in_desktops = False
    for line in COMMANDS_YAML.read_text(encoding='utf-8').splitlines():
        if re.match(r'^\S', line):
            in_desktops = line.strip() == 'desktops:'
            continue
        if in_desktops:
            match = re.match(r'^  ([A-Za-z0-9-]+):\s*$', line)
            if match:
                keys.add(match.group(1))
    return keys


def test_exactly_one_unit_is_shipped():
    """One file for every desktop: the policy lives in config.ini, not here."""
    assert [path.name for path in EXAMPLES] == [service.UNIT_NAME]


@pytest.mark.parametrize('path', EXAMPLES, ids=lambda path: path.stem)
def test_unit_has_required_directives(path):
    sections = parse_unit(path)
    assert {'Unit', 'Service', 'Install'} <= set(sections)
    assert values(sections, 'Unit', 'Description')
    assert values(sections, 'Unit', 'BindsTo')
    assert values(sections, 'Unit', 'After')
    # A stale SESSION_MANAGER inherited from an earlier KDE login would make Qt
    # open an ICE connection to a session that is not ours -- one more client
    # ksmserver waits for at logout.
    assert values(sections, 'Service', 'UnsetEnvironment') == ['SESSION_MANAGER']

    assert values(sections, 'Service', 'Type') == ['simple']
    assert values(sections, 'Service', 'ExecStart')
    assert values(sections, 'Install', 'WantedBy')


@pytest.mark.parametrize('path', EXAMPLES, ids=lambda path: path.stem)
def test_restart_never_answers_a_logout(path):
    """`Restart=always` is the logout bug: it respawns into a draining session.

    Measured in luks-tray, which shipped exactly that line: the session never
    emptied, the display manager never got the VT, and the logout stopped on a
    black screen.  `on-failure` still recovers a crash.
    """
    sections = parse_unit(path)
    assert values(sections, 'Service', 'Restart') == ['on-failure']
    prevent = values(sections, 'Service', 'RestartPreventExitStatus')
    assert len(prevent) == 1
    listed = set(prevent[0].split())
    assert listed >= PERMANENT_EXIT_STATUSES, prevent[0]
    assert listed >= STOP_SIGNALS, prevent[0]


@pytest.mark.parametrize('path', EXAMPLES, ids=lambda path: path.stem)
def test_the_stop_is_bounded(path):
    """The default 90s is how long a wedged Qt loop may hold a logout."""
    sections = parse_unit(path)
    timeout = seconds(values(sections, 'Service', 'TimeoutStopSec')[0])
    assert 0 < timeout <= 10, timeout
    assert values(sections, 'Service', 'SendSIGKILL') == ['yes']


@pytest.mark.parametrize('path', EXAMPLES, ids=lambda path: path.stem)
def test_restart_rate_limit_can_actually_trip(path):
    """RestartSec must not outpace the limiter, or the loop is endless."""
    sections = parse_unit(path)
    restart_sec = seconds(values(sections, 'Service', 'RestartSec')[0])
    burst = int(values(sections, 'Unit', 'StartLimitBurst')[0])
    interval = seconds(values(sections, 'Unit', 'StartLimitIntervalSec')[0])

    assert restart_sec > 0
    assert burst >= 2
    # `burst` restarts spaced `RestartSec` apart have to fit in the window,
    # otherwise systemd never reaches the burst and restarts forever.
    assert restart_sec * burst <= interval


def test_prevented_codes_match_the_exits_the_applet_uses():
    exits = set(re.findall(r'sys\.exit\((\d+)\)', MAIN.read_text(encoding='utf-8')))
    assert exits >= PERMANENT_EXIT_STATUSES, sorted(exits)


@pytest.mark.parametrize('path', EXAMPLES, ids=lambda path: path.stem)
def test_exec_start_is_the_launcher_and_nothing_session_shaped(path):
    """No `--de`, no `--desktops`: a desktop name here is a snapshot.

    `--svc-mode` is the opposite: a constant marker meaning "a unit started me",
    which is what makes the applet obey ``[ServiceAllowed]``.  An environment
    check cannot do that job -- ``INVOCATION_ID`` is inherited by every child of
    a unit, so a terminal opened inside one makes a hand-started pwr-tray look
    like the service.
    """
    sections = parse_unit(path)
    assert values(sections, 'Service', 'ExecStart') == [
        '%h/.local/bin/pwr-tray --svc-mode']


@pytest.mark.parametrize('path', EXAMPLES, ids=lambda path: path.stem)
def test_no_session_snapshot_is_pinned(path):
    """The unit may not remember *what session installed it*.

    A unit's own `Environment=` outranks what the session exports, so a pinned
    `XDG_SESSION_TYPE`, display or desktop name is a snapshot that survives the
    login it described -- it made Qt load the platform plugin of a session that
    had ended, and it would now also route the `[ServiceAllowed]` gate from the
    wrong desktop.  `pwr_tray/Session.py` re-derives all of them instead.
    """
    sections = parse_unit(path)
    assert values(sections, 'Unit', 'ConditionEnvironment') == []
    pinned = ' '.join(values(sections, 'Service', 'Environment'))
    for name in ('XDG_CURRENT_DESKTOP', 'XDG_SESSION_TYPE', 'WAYLAND_DISPLAY',
                 'DISPLAY', 'XAUTHORITY'):
        assert f'{name}=' not in pinned, (name, pinned)


@pytest.mark.parametrize('path', EXAMPLES, ids=lambda path: path.stem)
def test_the_unit_is_a_constant(path):
    """Installing it never records an installation or a session."""
    text = path.read_text(encoding='utf-8')
    assert service.shipped_unit_text() == text
    assert '--de' not in text
    assert service.installed_executable(text, {'HOME': '/home/user'}) == (
        '/home/user/.local/bin/pwr-tray')
    assert '%h/.config/pwr-tray' in text       # the log, not an install path


@pytest.mark.parametrize('path', EXAMPLES, ids=lambda path: path.stem)
def test_the_log_directory_is_one_svc_install_can_create(path):
    """systemd never creates it: a path it cannot resolve fails the unit (209).

    ``plan_install()`` reads these back out of the unit, so every shipped
    ``StandardOutput=``/``StandardError=`` file target has to expand to an
    absolute directory.  The target is the applet's own ``debug.log``, so the
    menu and ``--follow-log`` read the file systemd makes the applet write.
    """
    folders = service.output_dirs(path.read_text(encoding='utf-8'), {'HOME': '/home/user'})
    assert folders, 'no absolute StandardOutput=/StandardError= file target'
    assert all(folder.is_absolute() for folder in folders), folders
    assert folders == [Path('/home/user/.config/pwr-tray')], folders


def test_every_desktop_in_commands_yaml_can_be_allowed():
    """`[ServiceAllowed]` is seeded from the same names detection produces."""
    known = set(desktop_keys())
    assert known == set(service.known_desktops()), sorted(known)
    assert len(known) == 10, sorted(known)


def test_every_desktop_name_is_a_valid_config_option():
    """configparser lowercases option names; a key with `:` would not survive."""
    for name in desktop_keys():
        assert re.fullmatch(r'[a-z0-9][a-z0-9-]*', name), name
