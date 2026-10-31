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
import json
import os
from types import SimpleNamespace

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


class IniTool:
    """ Configued Params for this class"""
    def __init__(self, paths_only=False):
        self.defaults = {
            'Settings': {
                'debug_mode': False,
                'power_down': False,
                'turn_off_monitors': False,
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
