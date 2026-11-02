#!/usr/bin/env python3
"""
TBD
"""
# pylint: disable=invalid-name, broad-exception-caught
# pylint: disable=too-many-branches,too-many-statements
# pylint: disable=too-many-instance-attributes
# pylint: disable=consider-using-from-import

import configparser
import copy
import io
import json
import os
import re
from types import SimpleNamespace
from typing import ClassVar

from pwr_tray.Utils import LOG_OLD_SUFFIX, prt


def to_array(val_str):
    """Parse a list spec into a list of positive ints, or None if malformed.

    Expecting a string of form: "[1,2,...]" or just "20".  Values that are
    unparsable, not a list/int, or contain no positive int at all yield None,
    so callers can fall back to the configured default instead of storing a
    half-parsed value.
    """
    if isinstance(val_str, list):
        vals = val_str
    else:
        try:
            vals = json.loads(val_str)
        except (TypeError, ValueError):
            return None
    if isinstance(vals, int) and not isinstance(vals, bool):
        vals = [vals]
    if not isinstance(vals, list):
        return None
    rvs = []
    for val in vals:
        if isinstance(val, int) and not isinstance(val, bool) and val > 0:
            rvs.append(val)
    if not rvs:
        return None
    if len(rvs) == 1: # always want two
        rvs.append(rvs[0])
    return rvs


#: The config.ini section that decides which desktops may run the service.
#: It is deliberately NOT one of `get_selectors()`: it holds no applet
#: parameters, so `_parse_config()` never looks at it.
SERVICE_SECTION = 'ServiceAllowed'

#: Values `configparser.getboolean()` accepts as true, spelled out: the section
#: is meant to be edited by hand, so `yes`/`on`/`1` are answers too.  Anything
#: else -- including `false`, `no`, `off`, `0` and a typo -- means "not allowed".
_TRUE = frozenset({'true', 'yes', 'on', '1'})

#: Written once, when the section is created, so the file explains itself.
SERVICE_HEADER = (
    '[ServiceAllowed]',
    '# Which desktops may run pwr-tray as a systemd --user service.',
    '#   pwr-tray --svc-install    sets the desktop you are logged into to True',
    '#   pwr-tray --svc-uninstall  sets it back to False',
    '# Edit these by hand to allow or forbid others.  False means the service',
    '# starts and exits immediately: it is not a failure and it is not retried.',
)

_SECTION_RE = re.compile(r'^\s*\[(?P<name>[^\]]+)\]\s*(?:[#;].*)?$')
#: One `key = value` line in the section, with an optional trailing comment.
_ENTRY_RE = re.compile(
    r'^\s*(?P<key>[^=:#;\s][^=:]*?)\s*[=:]\s*(?P<value>[^#;]*?)\s*(?P<tail>[#;].*)?$')


def _as_bool(value):
    """Read one hand-written flag; anything unrecognised is False."""
    return (value or '').strip().lower() in _TRUE


def _section_bounds(lines, name):
    """Return `(header index, end index)` of `[name]`, or None when absent."""
    header = None
    for index, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if not match:
            continue
        if header is None:
            if match.group('name').strip().lower() == name.lower():
                header = index
        else:
            return header, index
    return (header, len(lines)) if header is not None else None


def parse_service_allowed(text):
    """Return `{desktop: bool}` from `[ServiceAllowed]`, in file order.

    A missing section, a missing key, a comment or an unparsable value all mean
    False: nothing is allowed until somebody says so.
    """
    allowed = {}
    lines = (text or '').splitlines()
    bounds = _section_bounds(lines, SERVICE_SECTION)
    if bounds is None:
        return allowed
    start, end = bounds
    for line in lines[start + 1:end]:
        entry = _ENTRY_RE.match(line)
        if entry:
            allowed[entry.group('key').strip().lower()] = _as_bool(entry.group('value'))
    return allowed


def set_flag_text(text, key, value, keys=()):
    """Return `text` with `key` set to `value` in `[ServiceAllowed]`.

    `configparser.write()` would rewrite the whole file and drop every comment,
    and this file is meant to be edited by hand, so the edit is surgical: the
    value token is replaced in place (an inline comment survives), a missing key
    is appended inside the section, and a missing section is created with the
    header that explains it.  `keys` supplies the other rows the new section
    gets, all False, so the list is complete the first time it appears.
    """
    word = 'True' if value else 'False'
    lines = (text or '').splitlines()
    bounds = _section_bounds(lines, SERVICE_SECTION)

    if bounds is None:
        if lines and lines[-1].strip():
            lines.append('')
        lines.extend(SERVICE_HEADER)
        for name in (keys or (key,)):
            lines.append(f'{name} = {word if name == key else "False"}')
        if key not in (keys or (key,)):
            lines.append(f'{key} = {word}')
        return '\n'.join(lines) + '\n'

    start, end = bounds
    pattern = re.compile(
        rf'^(?P<head>\s*{re.escape(key)}\s*[=:]\s*)(?P<value>[^#;]*?)(?P<tail>\s*(?:[#;].*)?)$',
        re.IGNORECASE)
    for index in range(start + 1, end):
        match = pattern.match(lines[index])
        if match:
            lines[index] = f'{match.group("head")}{word}{match.group("tail")}'
            return '\n'.join(lines) + '\n'

    # The key is new: keep it inside the section, after its last content line,
    # so a hand-written group of rows stays together.
    insert_at = end
    while insert_at - 1 > start and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, f'{key} = {word}')
    return '\n'.join(lines) + '\n'


def _default_text(value):
    """Return the config.ini text for one built-in default value."""
    return value if isinstance(value, str) else str(value)


def _present_keys(lines, bounds):
    """Return the lower-cased keys a section already holds.

    A *commented-out* row counts as present: `#key = value` is how a hand
    editor disables a setting on purpose, and writing it back live would
    silently undo that.
    """
    if bounds is None:
        return set()
    start, end = bounds
    names = set()
    for line in lines[start + 1:end]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped[0] in '#;':
            stripped = stripped[1:].lstrip()
        match = _ENTRY_RE.match(stripped)
        if match:
            names.add(match.group('key').strip().lower())
    return names


def _insert_rows(lines, bounds, rows):
    """Insert `rows` at the end of a section, before its trailing blanks."""
    start, end = bounds
    insert_at = end
    while insert_at - 1 > start and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines[insert_at:insert_at] = rows


def normalize_config_text(text, defaults, known_desktops=None):
    """Return `(text, changes)`: `text` plus the rows this version expects.

    Additive only.  Nothing already in the file is removed or rewritten, so
    ordering, comments, `key: value` separators and blank lines survive -- the
    same rule :func:`set_flag_text` follows, and the reason
    ``configparser.write()`` is never used here (it would reorder the file and
    delete every comment).

    Three decisions worth naming:

    * A key that is *present but commented out* counts as present, so a row the
      user disabled by hand is not re-added live.
    * ``[HiBattery]`` and ``[LoBattery]`` are never filled in.  Their rows are
      deliberately absent: an absent key inherits the user's ``[Settings]``
      value, while a key written into the tier wins outright (see
      :meth:`IniTool._tier_base`).  Materialising ``power_down = True`` into
      ``[LoBattery]`` would therefore power a machine off instead of suspending
      it for a user who wrote ``power_down = False`` in ``[Settings]`` -- the
      opposite of what that user asked for.
    * ``[ServiceAllowed]`` rows come from the supported-desktop list, which a
      release can change.  A missing row is added as ``False``, never ``True``:
      absence already means False, so this only makes the section complete and
      readable, and consent is never inferred.  A row naming a desktop this
      build no longer supports is left alone (see
      :func:`service_rows_not_supported`), because deleting it would revoke a
      permission as a side effect of a cosmetic edit.

    `changes` is empty when the file already holds everything, and the caller
    then writes nothing -- so a normalised file is byte-identical to itself on
    every later start.
    """
    if not text:
        return text, []
    lines = text.splitlines()
    trailing = text.endswith('\n')
    changes = []

    settings = dict(defaults.get('Settings', {}))
    bounds = _section_bounds(lines, 'Settings')
    if bounds is None:
        if lines and lines[-1].strip():
            lines.append('')
        lines.append('[Settings]')
        bounds = _section_bounds(lines, 'Settings')
        changes.append('created the [Settings] section')
    present = _present_keys(lines, bounds)
    rows = [f'{key} = {_default_text(value)}'
            for key, value in settings.items()
            if key.lower() not in present]
    if rows:
        _insert_rows(lines, bounds, rows)
        changes.extend(f'Settings: {row}' for row in rows)

    known = [str(name) for name in (known_desktops or [])]
    bounds = _section_bounds(lines, SERVICE_SECTION)
    if bounds is None:
        if known:
            if lines and lines[-1].strip():
                lines.append('')
            lines.extend(SERVICE_HEADER)
            lines.extend(f'{name} = False' for name in known)
            changes.append(f'created the [{SERVICE_SECTION}] section')
    else:
        present = _present_keys(lines, bounds)
        rows = [f'{name} = False' for name in known
                if name.lower() not in present]
        if rows:
            _insert_rows(lines, bounds, rows)
            changes.extend(f'{SERVICE_SECTION}: {row}' for row in rows)

    if not changes:
        return text, []
    return '\n'.join(lines) + ('\n' if trailing else ''), changes


def service_rows_not_supported(text, known_desktops):
    """Rows in ``[ServiceAllowed]`` naming a desktop this build does not know.

    Reported, never removed: the row may be a typo the user is debugging, or a
    permission they still want if that desktop comes back.
    """
    bounds = _section_bounds((text or '').splitlines(), SERVICE_SECTION)
    if bounds is None:
        return []
    known = {str(name).lower() for name in (known_desktops or [])}
    return [name for name in parse_service_allowed(text) if name not in known]


def default_config_text():
    """The text a brand-new config.ini starts from (the built-in defaults)."""
    parser = configparser.ConfigParser()
    parser.read_dict(IniTool.defaults)
    buffer = io.StringIO()
    parser.write(buffer)
    return buffer.getvalue()


class IniTool:
    """ Configued Params for this class"""
    #: Class level so `default_config_text()` can render a brand-new file
    #: without building an instance (and without touching $HOME to do it).
    defaults: ClassVar[dict] = {
        'Settings': {
            'debug_mode': False,
            'power_down': False,
            'turn_off_monitors': False,
            # Ask before the menu's log off / reboot / power off.  Only the
            # menu: the idle-driven power down (IdleStateMachine) is never
            # confirmed, because nobody is there to click twice (see
            # PwrTray.arm_or_run).
            'confirm_session_end': False,
            'lock_min_list': '[15, 30]',
            'sleep_min_list': '[5, 30]',
            'lo_battery_pct': 10,
            'gui_editor': 'geany',
            # Command prefix used to show debug.log in a terminal window,
            # e.g. 'xterm -e' or 'gnome-terminal --'; empty auto-detects.
            'log_viewer': '',

        }, 'HiBattery': { #was OnBattery
            'power_down': False,
            'lock_min_list': '[10, 20]',
            'sleep_min_list': '[1, 10]',

        }, 'LoBattery': {
            'power_down': True,
            'lock_min_list': '[1]',
            'sleep_min_list': '[1]',
        }
    }

    def __init__(self, paths_only=False):
        self.defaults = copy.deepcopy(self.__class__.defaults)
        self.folder = os.path.expanduser("~/.config/pwr-tray")
        self.ini_path =  os.path.join(self.folder, "config.ini")
        self.log_path =  os.path.join(self.folder, "debug.log")
        # The single rotated copy kept by Utils.rotate_log().
        self.log_old_path =  self.log_path + LOG_OLD_SUFFIX
        self.picks_path =  os.path.join(self.folder, "picks.json")
        self.config = configparser.ConfigParser()
        self.last_mod_time = None
        self.params_by_selector = {}
        self._config_problem = ''
        if not paths_only:
            self.ensure_ini_file()

    @staticmethod
    def get_selectors():
        """ Returns the in right "order" """
        return ['Settings', 'HiBattery', 'LoBattery']

    def the_default(self, selector, key):
        """ return the default value given the selector and key """
        default = self.defaults[selector][key]
        if key.endswith('_list'):
            # Defaults are stored as the literal config text so they can be
            # written with config.read_dict(); hand callers the parsed list so
            # a fallback never leaks the raw string into params.
            parsed = to_array(default)
            return default if parsed is None else parsed
        return default

    def with_list_defaults(self, selector, params):
        """Return `params` as a SimpleNamespace whose `_list` values are lists.

        A list key already holding a parsed list is kept as is, whether it came
        from this section or was inherited from [Settings].  A key still holding
        the raw default string (absent from the section) or None (a spec that
        failed to parse) is replaced by the selector's default list.
        """
        for key in self.defaults[selector]:
            if key.endswith('_list') and not isinstance(params[key], list):
                params[key] = self.the_default(selector, key)
        return SimpleNamespace(**params)

    def get_current_vals(self, selector, list_name):
        """ Expecting a list of two or more non-zero ints """
        if selector in self.params_by_selector and hasattr(self.params_by_selector[selector], list_name):
            vals = getattr(self.params_by_selector[selector], list_name)
            if isinstance(vals, list) and len(vals) >= 2:
                return vals
        return self.the_default(selector, list_name) # should not get here

    def get_rotated_vals(self, selector, list_name, first):
        """ TBD """
        vals = self.get_current_vals(selector, list_name)
        if first in vals:
            while vals[0] != first:
                vals = vals[1:] + vals[:1]
            setattr(self.params_by_selector[selector], list_name, vals)
        return vals

    def ensure_ini_file(self):
        """Create the config folder and file when missing; True if created.

        A read-only or unmounted config folder must not abort start-up: the
        applet can run on the built-in defaults, so the failure is logged and
        left to `update_config()` to report.
        """
        try:
            os.makedirs(self.folder, exist_ok=True)
            if os.path.exists(self.ini_path):
                return False
            self.config.read_dict(self.defaults)
            with open(self.ini_path, 'w', encoding='utf-8') as configfile:
                self.config.write(configfile)
        except OSError as exc:
            self._report_config_problem(f'cannot create ({exc})')
            return False
        return True

    def normalize_config(self, known_desktops=None):
        """Add the rows this version expects and an older file lacks.

        Called once at start-up, after the first read (see `main()`): doing it
        from `update_config()` would race the user's editor and change the very
        mtime the tick compares against.  Nothing is ever deleted -- unknown
        keys and sections are reported by `_parse_config()` and left in place.

        A missing or read-only file is not an error: the applet runs on the
        built-in defaults, so the failure is logged and start-up continues.
        Returns the list of changes, empty when the file was already complete
        (in which case it is not opened for writing at all).
        """
        try:
            with open(self.ini_path, encoding='utf-8') as handle:
                text = handle.read()
        except OSError as exc:
            prt(f'WARN: cannot normalise {self.ini_path} ({exc})')
            return []
        for name in service_rows_not_supported(text, known_desktops):
            prt(f'CONFIG: [{SERVICE_SECTION}] has {name!r}, which this build'
                ' does not support; leaving it alone')
        updated, changes = normalize_config_text(text, self.defaults,
                                                 known_desktops)
        if not changes:
            return []
        try:
            with open(self.ini_path, 'w', encoding='utf-8') as handle:
                handle.write(updated)
        except OSError as exc:
            prt(f'WARN: cannot write {self.ini_path} ({exc})')
            return []
        for change in changes:
            prt(f'CONFIG: added {change}')
        # The write moved the mtime the tick watches; remembering it keeps the
        # next tick from re-reading a file that only gained defaults.
        try:
            self.last_mod_time = os.path.getmtime(self.ini_path)
        except OSError:
            self.last_mod_time = None
        return changes

    def _report_config_problem(self, reason):
        """Warn about a config.ini problem once, not once per poll tick."""
        message = f'{self.ini_path}: {reason}'
        if message == self._config_problem:
            return
        self._config_problem = message
        fallback = ('last good values' if self.params_by_selector
                    else 'built-in defaults')
        prt(f'WARN: {message}; keeping {fallback}')

    def _read_ini_file(self):
        """Load config.ini into a fresh parser; False (after a warning) on error.

        Reading is a full replace, not a merge: a section removed from the file
        must not survive from an earlier read, and a partially parsed file must
        never reach `_parse_config()`.
        """
        parser = configparser.ConfigParser()
        try:
            with open(self.ini_path, encoding='utf-8') as handle:
                parser.read_file(handle)
        except (OSError, configparser.Error, UnicodeDecodeError) as exc:
            self._report_config_problem(f'cannot read ({exc})')
            return False
        self.config = parser
        return True

    def _use_fallback(self):
        """Build params from the defaults if nothing was parsed yet.

        Returns True when the defaults were used, so a first read failure still
        leaves callers with a complete parameter set instead of an empty
        `params_by_selector`.
        """
        if self.params_by_selector:
            return False
        self.config = configparser.ConfigParser()
        self.config.read_dict(self.defaults)
        self._parse_config()
        return True

    def update_config(self):
        """Check if the file has been modified since the last read.

        Returns True when `params_by_selector` was rebuilt.  This runs from a Qt
        slot on every tick, so it never raises: a deleted, unreadable or
        malformed config.ini keeps the last good values and is reported once per
        change rather than once per tick.
        """
        try:
            current_mod_time = os.path.getmtime(self.ini_path)
        except OSError as exc:
            self._report_config_problem(f'unavailable ({exc.strerror or exc})')
            return self._use_fallback()

        if current_mod_time == self.last_mod_time:
            return False # not updated

        # The stat succeeded: the file exists again, so a later problem is new
        # and deserves its own warning.
        self._config_problem = ''
        if not self._read_ini_file():
            # Remember the mtime: a malformed file is reported when it is
            # saved again, not on every tick.
            self.last_mod_time = current_mod_time
            return self._use_fallback()

        self.last_mod_time = current_mod_time
        self._parse_config()
        return True # updated

    def _tier_base(self, selector, settings_params, explicit_settings):
        """Start a tier from [Settings], preferring the tier's own defaults.

        A tier inherits every value the user actually wrote in [Settings]
        (v1 #4).  A key absent from both the file's [Settings] and the tier
        itself falls back to this tier's entry in `self.defaults`, so the
        per-tier table is authoritative instead of decorative.
        """
        base = copy.deepcopy(settings_params)
        for key in self.defaults[selector]:
            if key not in explicit_settings:
                base[key] = self.the_default(selector, key)
        return base

    def _apply_section(self, selector, params, goldens):
        """Overlay the user's values for `selector` onto `params`.

        Returns the set of keys whose file value was accepted.  A rejected
        value counts as absent, so callers can tell "the user chose this" from
        "this is only a default" -- which is what lets a tier fall back to its
        own default when [Settings] holds nothing usable for a key.
        """
        applied = set()
        if selector not in self.config:
            return applied

        # iterate the candidates
        candidates = dict(self.config[selector])
        for key, value in candidates.items():
            if key not in goldens:
                prt(f'skip {selector}.{key}: {value!r} [unknown key]')
                continue

            if key.endswith('_list'):
                list_value = to_array(value)
                if list_value is None:
                    # Malformed spec: keep a list already inherited from
                    # [Settings], otherwise fall back to the default list
                    # (with_list_defaults() also covers the missing-key case).
                    if isinstance(params[key], list):
                        prt(f'skip {selector}.{key}: {value!r}'
                            f' [bad list spec; keeping {params[key]}]')
                    else:
                        params[key] = self.the_default(selector, key)
                        prt(f'skip {selector}.{key}: {value!r}'
                            f' [bad list spec; using default {params[key]}]')
                else:
                    params[key] = list_value
                    applied.add(key)
                continue

            if isinstance(goldens[key], bool):
                if isinstance(value, str):
                    if value.lower() == 'true':
                        value = True
                    elif value.lower() == 'false':
                        value = False
                if isinstance(value, bool):
                    params[key] = value
                    applied.add(key)
                else:
                    prt(f'skip {selector}.{key}: {value!r} [expecting bool]')
                continue

            if isinstance(goldens[key], int):
                try:
                    params[key] = int(value)
                except (TypeError, ValueError):
                    prt(f'skip {selector}.{key}: {value!r} [expecting int repr]')
                else:
                    applied.add(key)
                continue

            if isinstance(goldens[key], str):
                if isinstance(value, str):
                    params[key] = value
                    applied.add(key)
                else:
                    prt(f'skip {selector}.{key}: {value!r} [expecting string]')
                continue

            # Unreachable for the built-in defaults; never assert here:
            # -O would remove the guard and the value would go unparsed.
            prt(f'skip {selector}.{key}: {value!r} [unhandled default type]')
            continue
        return applied

    def _parse_config(self):
        """Rebuild `params_by_selector` from the parsed `self.config`."""
        goldens = self.defaults['Settings']
        all_params = {}
        explicit_settings = set()
        settings_params = None

        # A section nobody reads is reported, not removed: the file is meant to
        # be edited by hand, and a section the user wrote (or a release left
        # behind) is theirs to keep.
        known_sections = set(self.get_selectors()) | {SERVICE_SECTION}
        for section in self.config.sections():
            if section not in known_sections:
                prt(f'skip [{section}]: unknown section [kept]')

        # Access the configuration values in order
        prt('parsing config.ini...')
        for selector in self.get_selectors():
            if selector == 'Settings':
                params = copy.deepcopy(goldens)
                explicit_settings = self._apply_section(selector, params,
                                                        goldens)
                settings_params = params
            else:
                # Tiers inherit the user's [Settings] values, but only the ones
                # the user actually wrote there; anything else uses the tier's
                # own default (see README: HiBattery and LoBattery inherit
                # missing/invalid settings from 'Settings').
                params = self._tier_base(selector, settings_params,
                                         explicit_settings)
                self._apply_section(selector, params, goldens)
            all_params[selector] = self.with_list_defaults(selector, params)

        self.params_by_selector = all_params

        prt('DONE parsing config.ini...')

        return True # updated
