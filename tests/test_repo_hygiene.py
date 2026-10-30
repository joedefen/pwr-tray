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


def test_the_coverage_gate_is_wired_and_cannot_quietly_disappear():
    """Audit F7: the suite reports coverage and enforces a floor.

    The gate is only real when all four links are present -- the dependency,
    the configuration, the CI command and the documented command.  Lose any one
    of them and "76% covered" decays silently back into "N tests ran".
    """
    pyproject = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))

    dev = pyproject['project']['optional-dependencies']['dev']
    assert any(requirement.startswith('pytest-cov') for requirement in dev), dev

    measure = pyproject['tool']['coverage']
    assert measure['run']['branch'] is True
    assert measure['run']['source'] == ['pwr_tray']
    floor = measure['report']['fail_under']
    # A floor is either a number that a red build can trip or it is decoration.
    assert 0 < floor <= 100, floor

    workflow = (ROOT / '.github' / 'workflows' / 'tests.yml').read_text(encoding='utf-8')
    assert 'pytest-cov' in workflow
    assert '--cov=pwr_tray' in workflow

    readme = (ROOT / 'README-DEV.md').read_text(encoding='utf-8')
    assert '--cov=pwr_tray' in readme

    # The data file belongs to the run, not to the repository.
    assert '.coverage' in (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()


def test_readme_matches_the_applet_on_chdir_and_single_instance():
    """Audit R4: the README promised two things the applet no longer does.

    It claimed the applet chdir()ed into the config folder, and that starting
    a second copy left you with two tray icons.  Both are pinned from each
    side here: the phrases must not come back, and the code must not start
    chdir()ing again (which is what made the first claim true once).
    """
    readme = (ROOT / 'README.md').read_text(encoding='utf-8').lower()
    assert 'changes directory to' not in readme
    assert 'two tray icons' not in readme

    sources = '\n'.join(path.read_text(encoding='utf-8')
                        for path in sorted((ROOT / 'pwr_tray').glob('*.py')))
    assert 'os.chdir' not in sources
