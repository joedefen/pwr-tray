"""Regression tests for repository hygiene.

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
    # A blind `except` (BLE) and bandit's checks (S) must stay gated: they are
    # the two rules that caught the most in this tree.
    assert {'BLE', 'S'} <= selected


def test_ci_runs_lint_tests_and_a_packaging_smoke_test():
    workflow = (ROOT / '.github' / 'workflows' / 'tests.yml').read_text(encoding='utf-8')
    assert 'ruff check' in workflow
    assert 'python -m pytest' in workflow
    assert 'python -m build' in workflow
    assert 'pwr-tray --version' in workflow


def test_the_coverage_gate_is_wired_and_cannot_quietly_disappear():
    """The suite reports coverage and enforces a floor.

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
    """The README must not promise two things the applet no longer does.

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


def test_the_release_gate_exists_and_the_documents_name_it():
    """One release entry point, and docs that point at the real one.

    The old six-step recipe uploaded `dist/*`, which accumulates every past
    version and aborts on the first `File already exists`.
    """
    gate = ROOT / 'bless'
    assert gate.is_file(), 'the release gate is missing'
    assert gate.stat().st_mode & 0o111, './bless must be executable'

    for document in ('README-DEV.md', 'pyproject.toml'):
        text = (ROOT / document).read_text(encoding='utf-8')
        assert './bless' in text, document
        assert './release' not in text, document


def test_no_documented_command_uploads_the_whole_dist_directory():
    # Split so this test's own source does not trip the scan it performs.
    needle = 'twine upload ' + 'dist/*'
    skipped = {'.git', 'build', 'dist', '.venv', '.ruff_cache', '.pytest_cache'}
    text_suffixes = {'', '.md', '.toml', '.yml', '.yaml', '.py', '.cfg', '.txt'}
    offenders = []
    for path in ROOT.rglob('*'):
        relative = path.relative_to(ROOT)
        if not path.is_file() or skipped & set(relative.parts):
            continue
        if path.suffix not in text_suffixes:
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        if needle in text:
            offenders.append(str(relative))
    assert not offenders, offenders


def test_the_documents_describe_the_testpypi_rehearsal():
    """Answering `test` at the prompt is documented, not tribal knowledge.

    The install line is the point of the rehearsal, and it is only correct with
    a cache purge and a fallback index, so the document has to carry all three.
    """
    readme = (ROOT / 'README-DEV.md').read_text(encoding='utf-8')
    assert '====> Enter test or publish or quit:' in readme
    assert 'test.pypi.org' in readme
    assert 'TWINE_TESTPYPI_PASSWORD' in readme
    assert '--extra-index-url' in readme


def test_ci_and_bless_share_the_distribution_inspector():
    workflow = (ROOT / '.github' / 'workflows' / 'tests.yml').read_text(encoding='utf-8')
    assert 'tests/inspect_dist.py' in workflow
    assert 'zipfile' not in workflow, 'the inline inspector must not come back'
