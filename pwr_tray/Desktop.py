#!/usr/bin/env python3
"""Desktop detection, command validation and resource installation.

These helpers used to live on ``PwrTray`` as static methods.  They are pure
functions over ``commands.yaml`` (plus ``$PATH`` and the config folder), so they
have no reason to sit inside the applet class: ``--list-des`` and the ``--svc-*``
verbs call them without ever constructing a tray.  ``PwrTray`` keeps thin
aliases so the public entry points (and the tests that pin them) do not move.
"""

import os
import re
import shutil

from ruamel.yaml import YAML

from pwr_tray import Utils
from pwr_tray.Errors import (
    DesktopDetectionError,
    MissingResourcesError,
)
from pwr_tray.Utils import prt


def load_de_config(config_dir):
    """Load DE commands YAML config.

    - Always copies default commands.yaml to config dir (so user sees latest)
    - If my-commands.yaml exists in config dir, loads that instead
    """
    yaml = YAML()
    Utils.copy_to_folder('commands.yaml', config_dir)
    user_yaml = os.path.join(config_dir, 'my-commands.yaml')
    if not os.path.exists(user_yaml):
        user_yaml = os.path.join(config_dir, 'commands.yaml')
    prt(f'loading DE config: {user_yaml}')
    with open(user_yaml, encoding='utf-8') as f:
        return dict(yaml.load(f))


def must_haves_for(de_json, compound_name):
    """Return the sorted commands a compound desktop name needs.

    The three config layers accumulate: defaults, session type, desktop.
    """
    _, _, stype = compound_name.rpartition('-')
    session_types = de_json.get('session_types', {})
    desktops = de_json.get('desktops', {})
    must = (de_json.get('defaults', {}).get('must_haves', [])
            + session_types.get(stype, {}).get('must_haves', [])
            + desktops.get(compound_name, {}).get('must_haves', []))
    return sorted(set(must))


def missing_commands(de_config):
    """Return the must_haves of `de_config` that are not on $PATH."""
    return sorted(cmd for cmd in set(de_config.get('must_haves', []))
                  if shutil.which(cmd) is None)


def build_variables(de_json, de_config):
    """Merge the desktop's commands over the defaults, then expand {{...}}.

    Split out of ``__init__`` so the action layer can be tested against the
    commands a desktop really gets, read from ``commands.yaml``, instead of
    against a fixture that can drift from it.
    """
    variables = dict(de_json['defaults'])
    for key in set(variables) - {'must_haves'}:
        if key in de_config:
            variables[key] = de_config[key]
    for key, value in list(variables.items()):
        if isinstance(value, str) and '{{' in value:
            variables[key] = re.sub(
                r'\{\{(\w+)\}\}',
                lambda match: variables.get(match.group(1), match.group(0)),
                value)
    return variables


def detect_de(de_json, force_de=None, log=prt):
    """Detect the current DE from environment using JSON config rules.
    Merges three layers: defaults -> session_type (x11/wayland) -> desktop.
    Returns merged config dict.  Use force_de to skip detection.

    Raises DesktopDetectionError when nothing matches: an assert would be
    stripped by python -O, leaving the app running with empty commands.

    `log` is the narration sink: the applet passes prt(), while the
    ``--svc-*`` verbs pass a no-op so their stdout stays machine-readable.
    """
    env = {key: os.environ.get(key, '')
           for key in ('XDG_CURRENT_DESKTOP', 'XDG_SESSION_DESKTOP',
                       'DESKTOP_SESSION', 'XDG_SESSION_TYPE')}
    lowered = {key: value.lower() for key, value in env.items()}
    session_type = lowered['XDG_SESSION_TYPE']
    desktop_str = ' '.join(lowered[key] for key in
                           ('XDG_CURRENT_DESKTOP', 'XDG_SESSION_DESKTOP',
                            'DESKTOP_SESSION'))

    log(f'ENV: XDG_CURRENT_DESKTOP={env["XDG_CURRENT_DESKTOP"]!r}'
        f' XDG_SESSION_DESKTOP={env["XDG_SESSION_DESKTOP"]!r}'
        f' DESKTOP_SESSION={env["DESKTOP_SESSION"]!r}'
        f' XDG_SESSION_TYPE={env["XDG_SESSION_TYPE"]!r}')

    session_types = de_json.get('session_types', {})
    desktops = de_json.get('desktops', {})
    known = list(desktops)

    def _merge(compound_name, entry):
        _, _, stype = compound_name.rpartition('-')
        scfg = session_types.get(stype, {})
        merged = dict(scfg)
        merged.update(entry)
        merged['name'] = compound_name
        merged['session_type'] = stype
        merged['must_haves'] = must_haves_for(de_json, compound_name)
        return merged

    # --de override: skip detection
    if force_de:
        if force_de not in desktops:
            raise DesktopDetectionError(
                f'--de {force_de!r} is not a known desktop',
                known_desktops=known, env=env)
        log(f'ENV: forced {force_de!r}')
        return _merge(force_de, desktops[force_de])

    # Desktop keys are compound: "kde-wayland", "i3-x11", etc.
    for compound_name, entry in desktops.items():
        detect, _, stype = compound_name.rpartition('-')
        if detect in desktop_str and stype == session_type:
            log(f'ENV: matched {compound_name!r}')
            return _merge(compound_name, entry)

    raise DesktopDetectionError(
        'no desktop environment matched the current session',
        known_desktops=known, env=env)


def ensure_resources(folder, resources):
    """Return absolute paths for `resources`, installing them if needed.

    Paths are always built from `folder`; a bare resource name would
    otherwise resolve against whatever directory the applet was started
    from.  A resource that is still absent after the copy is a startup
    error -- a short icon list used to surface much later as an
    IndexError from show_icon().
    """
    paths = []
    missing = []
    for resource in resources:
        path = os.path.join(folder, resource)
        if not os.path.isfile(path):
            try:
                Utils.copy_to_folder(resource, folder)
            except OSError as exc:
                prt(f'WARN: cannot copy {resource!r} to {folder!r}: {exc}')
        if os.path.isfile(path):
            paths.append(path)
        else:
            prt(f'WARN: cannot find {path!r}')
            missing.append(resource)
    if missing:
        raise MissingResourcesError(missing, folder)
    return paths
