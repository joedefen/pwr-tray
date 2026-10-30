"""Validate the systemd --user units shipped in pwr_tray/resources/systemd/.

These units are no longer documentation: they live inside the package, so the
wheel contains them, `pwr-tray --svc-install` copies one to
``~/.config/systemd/user/``, and systemd is what actually runs them on a
user's machine.  The checks here and in test_service_commands.py are the
reason that promise is safe: every unit must parse, carry the directives
systemd needs, name a desktop key that exists in ``commands.yaml``, describe
one consistent session, and still hold all of that after render() substitutes
this machine's executable and display into it.
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


def test_examples_are_present():
    assert len(EXAMPLES) == 10
    assert {path.name for path in EXAMPLES} == {
        'pwr-tray-cinnamon.service',
        'pwr-tray-hyprland.service',
        'pwr-tray-i3.service',
        'pwr-tray-kde-wayland.service',
        'pwr-tray-kde-x11.service',
        'pwr-tray-lxqt.service',
        'pwr-tray-mate.service',
        'pwr-tray-niri.service',
        'pwr-tray-sway.service',
        'pwr-tray-xfce.service',
    }


@pytest.mark.parametrize('path', EXAMPLES, ids=lambda path: path.stem)
def test_unit_has_required_directives(path):
    sections = parse_unit(path)
    assert {'Unit', 'Service', 'Install'} <= set(sections)
    assert values(sections, 'Unit', 'Description')
    assert values(sections, 'Unit', 'ConditionEnvironment')
    assert values(sections, 'Unit', 'BindsTo')
    assert values(sections, 'Unit', 'After')

    assert values(sections, 'Service', 'Type') == ['simple']
    assert values(sections, 'Service', 'Restart') == ['always']
    assert values(sections, 'Service', 'ExecStart')
    assert values(sections, 'Install', 'WantedBy')


@pytest.mark.parametrize('path', EXAMPLES, ids=lambda path: path.stem)
def test_permanent_exit_statuses_are_not_restarted(path):
    """A lock refusal (3) or a failed construction (2) must not respawn."""
    sections = parse_unit(path)
    prevent = values(sections, 'Service', 'RestartPreventExitStatus')
    assert len(prevent) == 1
    assert set(prevent[0].split()) >= PERMANENT_EXIT_STATUSES, prevent[0]


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
def test_exec_start_names_a_known_desktop(path):
    sections = parse_unit(path)
    exec_start = values(sections, 'Service', 'ExecStart')
    assert len(exec_start) == 1
    command = exec_start[0]

    assert 'pwr-tray' in command
    match = re.search(r'--de[ =]([A-Za-z0-9-]+)', command)
    assert match, command
    desktop = match.group(1)
    assert desktop in desktop_keys()


@pytest.mark.parametrize('path', EXAMPLES, ids=lambda path: path.stem)
def test_environment_matches_condition_and_desktop(path):
    sections = parse_unit(path)
    conditions = values(sections, 'Unit', 'ConditionEnvironment')
    assert len(conditions) == 1
    assert conditions[0].startswith('XDG_CURRENT_DESKTOP=')

    environment = dict(
        item.split('=', 1)
        for item in values(sections, 'Service', 'Environment')
        if '=' in item)
    assert environment['XDG_CURRENT_DESKTOP'] == conditions[0].split('=', 1)[1]

    exec_start = values(sections, 'Service', 'ExecStart')[0]
    desktop = re.search(r'--de[ =]([A-Za-z0-9-]+)', exec_start).group(1)
    assert desktop.endswith(f"-{environment['XDG_SESSION_TYPE']}")


def test_every_shipped_unit_maps_to_a_known_desktop():
    """--svc-install finds a unit by desktop name, so the mapping must be total."""
    found = service.templates(ROOT / 'pwr_tray' / 'resources' / 'systemd')
    assert len(found) == len(EXAMPLES) == 10, sorted(found)
    assert set(found) <= desktop_keys(), sorted(set(found) - desktop_keys())


@pytest.mark.parametrize('path', EXAMPLES, ids=lambda path: path.stem)
def test_render_replaces_only_the_executable(path):
    """Install-time substitution is a two-line operation, not a rewrite."""
    text = path.read_text(encoding='utf-8')
    rendered = service.render(text, service.desktop_of(text), '/opt/bin/pwr-tray', {})
    pairs = zip(text.splitlines(), rendered.splitlines(), strict=True)
    changed = [(before, after) for before, after in pairs if before != after]
    assert len(changed) == 1, changed
    assert changed[0][1].startswith('ExecStart=/opt/bin/pwr-tray ')


@pytest.mark.parametrize('path', EXAMPLES, ids=lambda path: path.stem)
def test_render_appends_the_live_display_override(path):
    """The pinned default stays readable; the live value is what systemd uses."""
    text = path.read_text(encoding='utf-8')
    desktop = service.desktop_of(text)
    key = 'WAYLAND_DISPLAY' if desktop.endswith('-wayland') else 'DISPLAY'
    rendered = service.render(text, desktop, '/opt/bin/pwr-tray', {key: 'live-9'})
    environment = values(parse_text(rendered), 'Service', 'Environment')
    pinned = [item for item in environment if item.startswith(f'{key}=')]
    assert len(pinned) == 2, pinned          # shipped default + live override
    assert pinned[-1] == f'{key}=live-9'     # last assignment wins in systemd


@pytest.mark.parametrize('path', EXAMPLES, ids=lambda path: path.stem)
def test_render_keeps_the_restart_policy(path):
    """The *installed* copy must fail fast on exit 2/3, exactly like the source."""
    text = path.read_text(encoding='utf-8')
    rendered = service.render(text, service.desktop_of(text), '/opt/bin/pwr-tray', {})
    sections = parse_text(rendered)
    prevent = values(sections, 'Service', 'RestartPreventExitStatus')
    assert len(prevent) == 1
    assert set(prevent[0].split()) >= PERMANENT_EXIT_STATUSES, prevent[0]
    assert values(sections, 'Unit', 'StartLimitBurst') == ['5']
