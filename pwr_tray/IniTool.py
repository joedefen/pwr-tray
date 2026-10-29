#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TBD
"""
# pylint: disable=invalid-name, broad-exception-caught
# pylint: disable=too-many-branches,too-many-statements
# pylint: disable=too-many-instance-attributes
# pylint: disable=consider-using-from-import
# pylint: disable=
# pylint: disable=

import os
import configparser
from types import SimpleNamespace
import copy
import json
from pwr_tray.Utils import prt


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
        except Exception:
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
            #   'dim_pct_brightness': 100,
            #   'dim_pct_lock_min': 100,

            }, 'HiBattery': { #was OnBattery
                'power_down': False,
                'lock_min_list': '[10, 20]',
                'sleep_min_list': '[1, 10]',
            #   'dim_pct_brightness': 50,
            #   'dim_pct_lock_min': 70,

            }, 'LoBattery': {
                'power_down': True,
                'lock_min_list': '[1]',
                'sleep_min_list': '[1]',
            #   'dim_pct_brightness': 50,
            #   'dim_pct_lock_min': 70,
            }
        }
        self.folder = os.path.expanduser("~/.config/pwr-tray")
        self.ini_path =  os.path.join(self.folder, "config.ini")
        self.log_path =  os.path.join(self.folder, "debug.log")
        self.picks_path =  os.path.join(self.folder, "picks.json")
        self.config = configparser.ConfigParser()
        self.last_mod_time = None
        self.section_params = {'Settings': {}, 'HiBattery': {}, 'LoBattery': {}, }
        self.params_by_selector = {}
        if not paths_only:
            self.ensure_ini_file()
            os.chdir(self.folder)

    @staticmethod
    def get_selectors():
        """ Returns the in right "order" """
        return 'Settings HiBattery LoBattery'.split()

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
        """Check if the config file exists, create it if not."""
        if not os.path.exists(self.folder):
            os.makedirs(self.folder, exist_ok=True)
        if not os.path.exists(self.ini_path):
            self.config.read_dict(self.defaults)
            with open(self.ini_path, 'w', encoding='utf-8') as configfile:
                self.config.write(configfile)

    def update_config(self):
        """ Check if the file has been modified since the last read """
        current_mod_time = os.path.getmtime(self.ini_path)
        if current_mod_time == self.last_mod_time:
            return False # not updated
        # Re-read the configuration file if it has changed
        self.config.read(self.ini_path)
        self.last_mod_time = current_mod_time

        goldens = self.defaults['Settings']
        running = goldens
        all_params = {}

        # Access the configuration values in order
        prt('parsing config.ini...')
        for selector in self.get_selectors():
            all_params[selector] = params = copy.deepcopy(running)
            # After [Settings] is parsed, the tiers inherit the user's values,
            # not the hard-coded defaults (see README: HiBattery and LoBattery
            # inherit missing/invalid settings from 'Settings').
            if selector == 'Settings':
                running = params
            if selector not in self.config:
                all_params[selector] = self.with_list_defaults(selector, params)
                continue

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
                    continue

                if isinstance(goldens[key], bool):
                    if isinstance(value, str):
                        if value.lower() == 'true':
                            value = True
                        elif value.lower() == 'false':
                            value = False
                    if isinstance(value, bool):
                        params[key] = value
                    else:
                        prt(f'skip {selector}.{key}: {value!r} [expecting bool]')
                    continue

                if isinstance(goldens[key], int):
                    try:
                        params[key] = int(value)
                        continue
                    except Exception:
                        prt(f'skip {selector}.{key}: {value!r} [expecting int repr]')
                        continue

                if isinstance(goldens[key], str):
                    if isinstance(value, str):
                        params[key] = value
                    else:
                        prt(f'skip {selector}.{key}: {value!r} [expecting string]')
                    continue

                assert False, f'unhandled goldens[{key}]: {value!r}'
            all_params[selector] = self.with_list_defaults(selector, params)

        self.params_by_selector = all_params

        prt('DONE parsing config.ini...')

        return True # updated
