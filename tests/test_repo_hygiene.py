"""Regression tests for audit finding 14 (repository hygiene).

The repository now carries its own lint configuration, a CI gate for lint,
tests and packaging, and ignores local virtualenvs.  These tests fail if any
of those artefacts disappears again.
"""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_virtualenvs_and_caches_are_ignored():
    lines = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
    for entry in ('.venv/', '.ruff_cache/', '.pytest_cache/'):
        assert entry in lines, entry


def test_ruff_configuration_lives_in_the_repository():
    tool = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['tool']
    ruff = tool['ruff']
    assert ruff['target-version'] == 'py311'
    selected = set(ruff['lint']['select'])
    # The audit's two headline findings must stay gated.
    assert {'BLE', 'S'} <= selected


def test_ci_runs_lint_tests_and_a_packaging_smoke_test():
    workflow = (ROOT / '.github' / 'workflows' / 'tests.yml').read_text(encoding='utf-8')
    assert 'ruff check' in workflow
    assert 'python -m pytest' in workflow
    assert 'python -m build' in workflow
    assert 'pwr-tray --version' in workflow
