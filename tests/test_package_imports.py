"""Regression tests for audit finding P1-5.

`pwr_tray/YamlDump.py` imported `LibGen.CustLogger`, a package that has never
been part of this project, so the module shipped in the wheel could not be
imported at all.  It and the unused `PrtUtil.py` have been deleted; these tests
keep a module with an unresolvable import from being packaged again.

Imports that need an optional runtime dependency (PyQt5, ruamel.yaml) are
satisfied by the stubs in the root conftest.py, so the suite still runs in a
bare container.
"""

import importlib
import pkgutil

import pwr_tray


def module_names():
    """Names of every importable module directly under the pwr_tray package."""
    return sorted(f'{pwr_tray.__name__}.{mod.name}'
                  for mod in pkgutil.iter_modules(pwr_tray.__path__))


def test_package_has_expected_modules():
    names = module_names()
    assert 'pwr_tray.main' in names
    assert 'pwr_tray.IniTool' in names
    assert 'pwr_tray.SwayIdleMgr' in names
    assert 'pwr_tray.Utils' in names


def test_every_packaged_module_imports():
    """Every module in the package must import without external project deps."""
    for name in module_names():
        importlib.import_module(name)


def test_dead_modules_are_not_packaged():
    """The unused, unimportable modules must not come back."""
    names = module_names()
    assert 'pwr_tray.YamlDump' not in names
    assert 'pwr_tray.PrtUtil' not in names
