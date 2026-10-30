"""Regression tests for audit finding 12 (metadata drift).

The published metadata once described a PyQt5 applet as a GTK one, carried a
dependency that could never apply, and exposed no version at all.  These tests
pin the package metadata to the code so the two cannot drift apart again.
"""

import sys
import tomllib
from pathlib import Path

import pytest

import pwr_tray

ROOT = Path(__file__).resolve().parent.parent


def load_pyproject():
    with open(ROOT / 'pyproject.toml', 'rb') as handle:
        return tomllib.load(handle)


def test_version_comes_from_the_package_only():
    """`pwr_tray.__version__` is the single authority on the project version."""
    project = load_pyproject()['project']
    assert 'version' not in project, "pyproject must not duplicate the version"
    assert 'version' in project['dynamic'], "flit must read __version__ from the package"
    assert pwr_tray.__version__


def test_package_version_is_valid_pep440():
    """The single source must still be a version a build backend can publish."""
    from packaging.version import Version

    assert str(Version(pwr_tray.__version__)) == pwr_tray.__version__


def test_summary_names_the_real_toolkit():
    description = load_pyproject()['project']['description'].lower()
    assert 'gtk' not in description
    assert 'pyqt5' in description


def test_python_baseline_is_311_or_newer():
    requires_python = load_pyproject()['project']['requires-python']
    assert requires_python == '>=3.11'


def test_no_unreachable_importlib_metadata_dependency():
    """A python_version<"3.8" marker is dead weight with requires-python >=3.11."""
    dependencies = load_pyproject()['project']['dependencies']
    assert not any('importlib-metadata' in dep for dep in dependencies)


def test_niri_keyword_is_documented():
    project = load_pyproject()['project']
    readme = (ROOT / 'README.md').read_text(encoding='utf-8').lower()
    assert 'niri' in project['keywords']
    assert 'niri' in readme


def test_main_does_not_inject_system_dist_packages():
    """A pipx venv must not silently shadow its packages with system ones."""
    source = (ROOT / 'pwr_tray' / 'main.py').read_text(encoding='utf-8')
    assert 'dist-packages' not in source
    assert 'sys.path.append' not in source


def test_version_flag_prints_the_package_version(monkeypatch, capsys):
    from pwr_tray import main as main_module

    monkeypatch.setattr(sys, 'argv', ['pwr-tray', '--version'])
    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    assert exc_info.value.code == 0
    assert pwr_tray.__version__ in capsys.readouterr().out
