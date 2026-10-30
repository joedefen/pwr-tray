"""Validate the systemd --user examples shipped in docs/examples/.

The README has always called these units "best-effort / untested".  These
tests are the missing check: every example must parse as a unit file, contain
the directives systemd needs to start it, point at a desktop key that exists in
``commands.yaml``, and agree with itself about the session type it describes.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = sorted((ROOT / 'docs' / 'examples').glob('*.service'))
COMMANDS_YAML = ROOT / 'pwr_tray' / 'resources' / 'commands.yaml'


def parse_unit(path):
    """Return {section: [(key, value), ...]} for a systemd unit file.

    ``configparser`` cannot be used directly: unit files repeat keys such as
    ``After=`` and comments are allowed at any indentation.
    """
    sections = {}
    current = None
    for raw in path.read_text(encoding='utf-8').splitlines():
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


def values(sections, section, key):
    return [value for found, value in sections.get(section, []) if found == key]


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
