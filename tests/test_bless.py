"""Tests for the release gate: `./bless` and the shared distribution inspector.

No network, no builds, no uploads.  What is exercised here is the part that
must not be wrong when the command is run for real: the duplicate check (in
both modes, before anything is built), the pure branch decision, the dry run's
refusal to reach an irreversible command, and the inspector's ability to
reject a wheel that lies about itself.
"""

import importlib.machinery
import importlib.util
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_module(name, path):
    """Load a script that has no `.py` suffix (`bless`) or is not installed."""
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


bless = load_module('bless_gate', ROOT / 'bless')
inspect_dist = load_module('inspect_dist_under_test', ROOT / 'tests' / 'inspect_dist.py')

PROJECT = inspect_dist.project()
VERSION = inspect_dist.expected_version()


def pypi_document(*versions, summary='A GTK tray applet for power management'):
    return {
        'info': {
            'summary': summary,
            'requires_python': '>=3.8',
            'requires_dist': ['pydbus>=0.6.0'],
            'keywords': 'power, energy',
        },
        'releases': {version: [] for version in versions},
    }


# --- the duplicate check ----------------------------------------------------

def test_a_published_version_is_refused_with_the_whole_release_list():
    with pytest.raises(bless.BlessError) as caught:
        bless.check_index('1.0.12', pypi_document('0.1', '1.0.10', '1.0.12'))

    message = str(caught.value)
    assert 'already on PyPI' in message
    # The user asked for the releases in the error, not just the collision.
    for published in ('0.1', '1.0.10', '1.0.12'):
        assert published in message


def test_an_unpublished_version_is_accepted_and_versions_sort_numerically():
    published = bless.check_index('2.0.0', pypi_document('1.0.9', '1.0.10', '0.9'))

    assert published == ['0.9', '1.0.9', '1.0.10']


def test_the_index_check_runs_before_any_build_or_upload(monkeypatch, capsys):
    """Both modes pay the duplicate check, and they pay it first."""
    calls = []
    monkeypatch.setattr(bless, 'preflight',
                        lambda version: {'branch': 'dev', 'sha': 'deadbee'})
    monkeypatch.setattr(bless, 'check_index',
                        lambda version: (_ for _ in ()).throw(
                            bless.BlessError('already published')))
    for name in ('gates', 'build', 'inspect_dist', 'check', 'smoke',
                 'branch_stage', 'tag', 'upload', 'verify_index', 'publish_git'):
        monkeypatch.setattr(bless, name, lambda *a, _n=name, **k: calls.append(_n))

    assert bless.main([]) == 1
    assert calls == []
    assert 'already published' in capsys.readouterr().err


def test_a_missing_index_is_a_refusal_and_not_a_blind_upload(monkeypatch):
    monkeypatch.setattr(bless, 'fetch_index',
                        lambda *a, **k: (_ for _ in ()).throw(
                            bless.BlessError('offline')))

    with pytest.raises(bless.BlessError):
        bless.check_index('2.0.0')


# --- dry run vs publish -----------------------------------------------------

def stub_stages(monkeypatch, calls, plan=None):
    monkeypatch.setattr(bless, 'preflight',
                        lambda version: {'branch': 'dev', 'sha': 'deadbee'})
    monkeypatch.setattr(bless, 'check_index', lambda version: ['1.0.12'])
    monkeypatch.setattr(bless, 'gates', lambda: calls.append('gates'))
    monkeypatch.setattr(bless, 'build', lambda version: (
        ROOT / 'build' / 'pwr_tray-2.0.0-py3-none-any.whl',
        ROOT / 'build' / 'pwr_tray-2.0.0.tar.gz'))
    monkeypatch.setattr(bless, 'inspect_dist', lambda w, s: calls.append('inspect'))
    monkeypatch.setattr(bless, 'check', lambda w, s: calls.append('check'))
    monkeypatch.setattr(bless, 'smoke', lambda w, v: calls.append('smoke'))
    monkeypatch.setattr(bless, 'branch_stage',
                        lambda branch: plan or {'align': 'main'})
    monkeypatch.setattr(bless, 'align_branches', lambda p, b: calls.append('align'))
    monkeypatch.setattr(bless, 'tag', lambda v: calls.append('tag'))
    monkeypatch.setattr(bless, 'upload', lambda w, s, v: calls.append('upload'))
    monkeypatch.setattr(bless, 'verify_index', lambda v, w: calls.append('verify'))
    monkeypatch.setattr(bless, 'publish_git',
                        lambda v, b, o: calls.append('push'))


def test_the_default_mode_is_a_dry_run_that_touches_nothing(monkeypatch, capsys):
    calls = []
    stub_stages(monkeypatch, calls)

    assert bless.main([]) == 0
    assert 'gates' in calls and 'inspect' in calls and 'smoke' in calls
    assert not {'align', 'tag', 'upload', 'verify', 'push'} & set(calls)

    output = capsys.readouterr().out
    assert 'DRY RUN' in output
    assert './bless publish' in output
    assert 'not published' in output


def test_publish_reaches_the_irreversible_stages_in_order(monkeypatch):
    calls = []
    stub_stages(monkeypatch, calls)

    assert bless.main(['publish']) == 0
    assert calls.index('align') < calls.index('tag') < calls.index('upload')
    assert calls.index('upload') < calls.index('verify') < calls.index('push')


def test_upload_refuses_without_a_token_before_starting_twine(monkeypatch):
    monkeypatch.delenv('TWINE_PASSWORD', raising=False)
    monkeypatch.setattr(bless, '_pypirc_credentials', lambda *a, **k: None)
    monkeypatch.setattr(bless, 'run',
                        lambda *a, **k: pytest.fail('twine must not start'))

    with pytest.raises(bless.BlessError) as caught:
        bless.upload(ROOT / 'x.whl', ROOT / 'x.tar.gz', VERSION)
    assert 'TWINE_PASSWORD' in str(caught.value)
    # The refusal says where a credential may come from, in both ways.
    assert '~/.pypirc' in str(caught.value)


# --- the TestPyPI rehearsal -------------------------------------------------

def test_the_test_version_starts_at_99_0():
    document = pypi_document('1.0.12')

    assert bless.next_test_version('1.0.12', document) == '1.0.12.99.0'
    assert bless.next_test_version(VERSION, {'releases': {}}) == f'{VERSION}.99.0'


def test_the_test_version_is_one_past_the_highest_suffix():
    """`{N}` is one greater than any other `{N}` the index already has."""
    document = pypi_document('2.0.0.99.0', '2.0.0.99.4', '2.0.0.99.1', '2.0.0')

    assert bless.next_test_version('2.0.0', document) == '2.0.0.99.5'


def test_the_test_counter_belongs_to_its_own_base_version():
    document = pypi_document('2.0.0.99.9', '1.0.12.99.7', '2.0.0.99.2')

    # Numerically, not as strings: 9 is past 2, so the next suffix is 10.
    assert bless.next_test_version('2.0.0', document) == '2.0.0.99.10'
    assert bless.next_test_version('1.0.12', document) == '1.0.12.99.8'
    # A base the index has never seen starts over rather than inheriting 11.
    assert bless.next_test_version('3.0.0', document) == '3.0.0.99.0'


def test_a_404_is_an_empty_index_because_the_project_may_be_new(monkeypatch):
    import urllib.error
    import urllib.request

    def missing(*args, **kwargs):
        raise urllib.error.HTTPError(bless.TESTPYPI_URL, 404, 'Not Found', {}, None)

    monkeypatch.setattr(urllib.request, 'urlopen', missing)
    assert bless.fetch_index(bless.TESTPYPI) == {'releases': {}}


def test_a_broken_index_is_still_a_refusal(monkeypatch):
    import urllib.error
    import urllib.request

    def unavailable(*args, **kwargs):
        raise urllib.error.HTTPError(bless.PYPI_URL, 503, 'Unavailable', {}, None)

    monkeypatch.setattr(urllib.request, 'urlopen', unavailable)
    with pytest.raises(bless.BlessError, match='duplicate check'):
        bless.fetch_index(bless.PYPI)


def test_each_target_gets_its_own_url_and_token(monkeypatch):
    monkeypatch.setenv('TWINE_PASSWORD', 'pypi-token')
    monkeypatch.setenv(bless.TESTPYPI['token'], 'testpypi-token')
    seen = []

    def fake_run(command, **kwargs):
        seen.append((command, kwargs.get('env') or {}))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bless, 'run', fake_run)
    bless.upload(ROOT / 'x.whl', ROOT / 'x.tar.gz', VERSION, bless.PYPI)
    bless.upload(ROOT / 'x.whl', ROOT / 'x.tar.gz', f'{VERSION}.99.0', bless.TESTPYPI)

    real, rehearsal = seen
    assert '--repository-url' not in real[0]
    assert bless.TESTPYPI_UPLOAD in rehearsal[0]
    assert real[1]['TWINE_USERNAME'] == rehearsal[1]['TWINE_USERNAME'] == '__token__'


def test_the_rehearsal_needs_its_own_testpypi_token(monkeypatch):
    monkeypatch.delenv(bless.TESTPYPI['token'], raising=False)
    monkeypatch.setattr(bless, '_pypirc_credentials', lambda *a, **k: None)
    monkeypatch.setattr(bless, 'run',
                        lambda *a, **k: pytest.fail('twine must not start'))

    with pytest.raises(bless.BlessError) as caught:
        bless.upload(ROOT / 'x.whl', ROOT / 'x.tar.gz', f'{VERSION}.99.0',
                     bless.TESTPYPI)
    assert bless.TESTPYPI['token'] in str(caught.value)


PYPIRC = """\
[distutils]
index-servers =
    pypi
    testpypi

[pypi]
repository = https://upload.pypi.org/legacy/
username = __token__
password = pypi-real

[testpypi]
repository = https://test.pypi.org/legacy/
username = __token__
password = pypi-rehearsal
"""


def test_a_pypirc_section_is_found_by_its_repository_url(tmp_path):
    path = tmp_path / '.pypirc'
    path.write_text(PYPIRC, encoding='utf-8')

    # The section has a name, but the URL is what decides: this is why the file
    # a user already keeps for twine works without renaming anything.
    assert bless._pypirc_credentials(bless.PYPI, path) == ('__token__', 'pypi-real')
    assert bless._pypirc_credentials(bless.TESTPYPI, path) == (
        '__token__', 'pypi-rehearsal')


def test_a_pypirc_for_another_index_is_not_a_credential(tmp_path):
    path = tmp_path / '.pypirc'
    path.write_text(PYPIRC.replace('https://test.pypi.org/legacy/',
                                   'https://example.invalid/legacy/'),
                    encoding='utf-8')

    assert bless._pypirc_credentials(bless.TESTPYPI, path) is None
    assert bless._pypirc_credentials(bless.TESTPYPI, tmp_path / 'absent') is None


def test_the_rehearsal_never_spends_the_pypi_token(monkeypatch):
    """Both can be configured at once; only one of them may reach TestPyPI."""
    monkeypatch.setenv('TWINE_PASSWORD', 'pypi-token-from-the-shell')
    monkeypatch.delenv(bless.TESTPYPI['token'], raising=False)
    monkeypatch.setattr(bless, '_pypirc_credentials',
                        lambda *a, **k: ('__token__', 'pypi-rehearsal'))
    seen = []

    def fake_run(command, **kwargs):
        seen.append((command, kwargs.get('env') or {}))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bless, 'run', fake_run)
    bless.upload(ROOT / 'x.whl', ROOT / 'x.tar.gz', f'{VERSION}.99.0',
                 bless.TESTPYPI)

    command, environment = seen[0]
    assert bless.TESTPYPI_UPLOAD in command
    assert environment['TWINE_PASSWORD'] == 'pypi-rehearsal'
    assert environment['TWINE_USERNAME'] == '__token__'


def test_the_environment_token_wins_over_the_file(monkeypatch):
    monkeypatch.setenv(bless.TESTPYPI['token'], 'from-the-shell')
    monkeypatch.setattr(bless, '_pypirc_credentials',
                        lambda *a, **k: ('__token__', 'from-the-file'))
    seen = []

    def fake_run(command, **kwargs):
        seen.append(kwargs.get('env') or {})
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bless, 'run', fake_run)
    bless.upload(ROOT / 'x.whl', ROOT / 'x.tar.gz', VERSION, bless.TESTPYPI)

    assert seen[0]['TWINE_PASSWORD'] == 'from-the-shell'


def test_the_throwaway_version_is_written_into_a_copy(tmp_path):
    package = tmp_path / 'pwr_tray'
    package.mkdir()
    (package / '__init__.py').write_text('__version__ = "2.0.0"\n')
    original = (ROOT / 'pwr_tray' / '__init__.py').read_text()

    bless._set_version(tmp_path, f'{VERSION}.99.0')

    rewritten = (package / '__init__.py').read_text()
    assert rewritten == f"__version__ = '{VERSION}.99.0'\n"
    # The tree keeps the version it is holding; only the copy is spent.
    assert (ROOT / 'pwr_tray' / '__init__.py').read_text() == original


def test_a_copy_without_a_version_line_is_refused(tmp_path):
    (tmp_path / 'pwr_tray').mkdir()
    (tmp_path / 'pwr_tray' / '__init__.py').write_text('"""no version"""\n')

    with pytest.raises(bless.BlessError, match='__version__'):
        bless._set_version(tmp_path, f'{VERSION}.99.0')


def stub_rehearsal(monkeypatch, calls):
    """Record the stages `bless test` reaches, and the version it passes on."""
    candidate = f'{VERSION}.99.4'
    artifacts = (ROOT / 'build' / f'pwr_tray-{candidate}-py3-none-any.whl',
                 ROOT / 'build' / f'pwr_tray-{candidate}.tar.gz')
    monkeypatch.setattr(bless, 'preflight',
                        lambda version, **kw: {'branch': 'dev', 'sha': 'deadbee'})
    monkeypatch.setattr(
        bless, 'fetch_index',
        lambda target=bless.PYPI: pypi_document(f'{VERSION}.99.3'))
    monkeypatch.setattr(bless, 'gates', lambda: calls.append('gates'))
    monkeypatch.setattr(bless, 'build_for_test',
                        lambda v: calls.append(('build', v)) or artifacts)
    monkeypatch.setattr(bless, 'inspect_dist',
                        lambda w, s, v=None: calls.append(('inspect', v)))
    monkeypatch.setattr(bless, 'check', lambda w, s: calls.append('check'))
    monkeypatch.setattr(bless, 'smoke', lambda w, v: calls.append(('smoke', v)))
    monkeypatch.setattr(bless, 'upload',
                        lambda w, s, v, target=bless.PYPI:
                        calls.append(('upload', v, target)))
    monkeypatch.setattr(bless, 'verify_index',
                        lambda v, w, target=bless.PYPI:
                        calls.append(('verify', v, target)))
    for name in ('tag', 'untag'):
        monkeypatch.setattr(bless, name, lambda v, _n=name: calls.append(_n))
    monkeypatch.setattr(bless, 'align_branches', lambda p, b: calls.append('align'))
    monkeypatch.setattr(bless, 'publish_git', lambda *a: calls.append('push'))


def test_test_mode_uploads_a_dotted_version_to_testpypi_only(monkeypatch, capsys):
    calls = []
    stub_rehearsal(monkeypatch, calls)

    assert bless.main(['test']) == 0

    candidate = f'{VERSION}.99.4'
    assert ('build', candidate) in calls
    assert ('inspect', candidate) in calls
    assert ('smoke', candidate) in calls
    assert ('upload', candidate, bless.TESTPYPI) in calls
    assert ('verify', candidate, bless.TESTPYPI) in calls
    # The rehearsal leaves git alone: no tag, no branch, no push.
    reached = {call for call in calls if isinstance(call, str)}
    assert not {'tag', 'untag', 'align', 'push'} & reached
    assert 'pip cache purge' in capsys.readouterr().out


def test_test_and_publish_are_exclusive(monkeypatch):
    monkeypatch.setattr(bless, 'preflight', lambda *a, **k: pytest.fail('reached'))
    with pytest.raises(SystemExit):
        bless.main(['test', 'publish'])


def test_the_install_hint_purges_the_cache_and_falls_back_to_pypi(capsys):
    candidate = f'{VERSION}.99.0'

    bless.install_hint(candidate)

    out = capsys.readouterr().out
    assert 'pip cache purge' in out
    assert '--index-url https://test.pypi.org/simple/' in out
    assert '--extra-index-url https://pypi.org/simple/' in out
    assert f'{bless.NAME}=={candidate}' in out
    assert 'not on PyPI' in out


# --- reuse: the identity is read from pyproject.toml, not written in here ----

def make_project(root, name, *, module=None):
    """A throwaway tree shaped the way `identity()` expects to find one."""
    root.mkdir(parents=True, exist_ok=True)
    package = module or name.replace('-', '_').replace('.', '_')
    (root / package).mkdir()
    (root / package / '__init__.py').write_text('__version__ = "1.2.3"\n')
    table = ['[project]', f'name = "{name}"', 'dynamic = ["version"]']
    if module:
        table += ['', '[tool.bless]', f'module = "{module}"']
    (root / 'pyproject.toml').write_text('\n'.join(table) + '\n')
    return root


def test_the_identity_is_read_from_the_project_table(tmp_path):
    told = bless.identity(make_project(tmp_path / 'plain', 'some-proj'))

    assert told['name'] == 'some-proj'
    # `-` becomes `_`: the name is written once, the package derived from it.
    assert told['module'] == 'some_proj'
    assert told['console'] is None
    assert told['inspector'] == 'tests/inspect_dist.py'
    assert told['smoke_args'] == [] and told['smoke_expect'] is None


def test_the_tool_table_overrides_the_derivation(tmp_path):
    root = make_project(tmp_path / 'odd', 'some-proj', module='odd_name')

    assert bless.identity(root)['module'] == 'odd_name'


def test_a_missing_package_is_refused_with_both_remedies(tmp_path):
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "nope"\n')

    with pytest.raises(bless.BlessError) as caught:
        bless.identity(tmp_path)
    message = str(caught.value)
    assert 'nope/__init__.py' in message
    assert '[tool.bless]' in message and 'module = "the_package"' in message


def test_a_tree_without_a_pyproject_is_refused(tmp_path):
    with pytest.raises(bless.BlessError, match=r'pyproject\.toml'):
        bless.identity(tmp_path)


def test_a_project_without_a_name_is_refused(tmp_path):
    (tmp_path / 'pyproject.toml').write_text('[tool.other]\nx = 1\n')

    with pytest.raises(bless.BlessError, match=r'\[project\] name'):
        bless.identity(tmp_path)


def test_a_package_without_a_version_line_is_refused(tmp_path):
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "quiet"\n')
    (tmp_path / 'quiet').mkdir()
    (tmp_path / 'quiet' / '__init__.py').write_text('"""no version here"""\n')

    with pytest.raises(bless.BlessError, match='__version__'):
        bless.identity(tmp_path)


def test_the_version_is_read_without_importing_the_package(tmp_path):
    root = make_project(tmp_path / 'read', 'readable')

    assert bless.read_version(root, 'readable') == '1.2.3'


def test_this_tree_derives_its_own_identity():
    name = PROJECT['name']
    index = f'https://pypi.org/pypi/{name}/json'
    rehearsal = f'https://test.pypi.org/pypi/{name}/json'

    assert bless.PROJECT['name'] == name
    assert bless.PROJECT['module'] == name.replace('-', '_')
    assert bless.PROJECT['console'] in list(PROJECT['scripts'])
    assert index == bless.PYPI_URL
    assert rehearsal == bless.TESTPYPI_URL
    assert bless.read_version() == VERSION


def test_the_refusal_names_the_project_and_where_the_version_lives():
    with pytest.raises(bless.BlessError) as caught:
        bless.check_index(VERSION, pypi_document(VERSION))
    message = str(caught.value)
    assert f'{bless.NAME} {VERSION}' in message
    assert f'{bless.MODULE}/__init__.py' in message


def test_the_gate_compiles_and_covers_the_derived_package(monkeypatch):
    commands = []

    def fake_run(command, **kwargs):
        commands.append([str(part) for part in command])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bless, 'run', fake_run)
    bless.gates()

    compiling = next(argv for argv in commands if 'compileall' in argv)
    assert bless.MODULE in compiling
    testing = next(argv for argv in commands if 'pytest' in argv)
    assert f'--cov={bless.MODULE}' in testing


def test_the_smoke_run_starts_the_declared_console_script(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        argv = [str(part) for part in command]
        calls.append(argv)
        if argv[-1] == '--version':
            printed = f'{VERSION}\n'
        elif argv[-1] == bless.PROJECT['smoke_args'][0]:
            printed = f'{bless.PROJECT["smoke_expect"]}\n'
        else:
            printed = ''
        return subprocess.CompletedProcess(argv, 0, stdout=printed, stderr='')

    monkeypatch.setattr(bless, 'run', fake_run)
    bless.smoke(ROOT / 'build' / 'x.whl', VERSION)

    wanted = bless.PROJECT['smoke_args'][0]
    reported = next(argv for argv in calls if argv[-1] == '--version')
    extra = next(argv for argv in calls if argv[-1] == wanted)

    assert reported[0].endswith(bless.PROJECT['console'])
    assert extra[1] == wanted


def test_the_docstring_states_what_reuse_requires():
    doc = bless.__doc__
    assert 'How to reuse' in doc
    for requirement in ('[project] name', '`-` and `.`', '[tool.bless] module',
                        '[project.scripts]', 'tests/inspect_dist.py',
                        '__version__', 'pytest --cov=<package>'):
        assert requirement in doc, requirement


# --- the branch model -------------------------------------------------------

def make_is_ancestor(edges):
    """`edges` maps a commit to its parent, enough to express the table."""
    def is_ancestor(older, newer):
        seen = set()
        cursor = newer
        while cursor and cursor not in seen:
            if cursor == older:
                return True
            seen.add(cursor)
            cursor = edges.get(cursor)
        return False
    return is_ancestor


def tips(main, dev, origin_main=None, origin_dev=None):
    return {'main': main, 'dev': dev,
            'origin/main': origin_main or main,
            'origin/dev': origin_dev or dev}


def test_branch_plan_fast_forwards_main_when_dev_is_ahead():
    plan = bless.branch_plan('dev', tips('a', 'b'), make_is_ancestor({'b': 'a'}))
    assert plan == {'align': 'main'}


def test_branch_plan_fast_forwards_dev_when_main_is_ahead():
    plan = bless.branch_plan('main', tips('b', 'a'), make_is_ancestor({'b': 'a'}))
    assert plan == {'align': 'dev'}


def test_branch_plan_does_nothing_when_the_branches_match():
    plan = bless.branch_plan('main', tips('a', 'a'), make_is_ancestor({}))
    assert plan == {'align': None}


def test_branch_plan_refuses_diverged_branches():
    plan = bless.branch_plan('main', tips('x', 'y'),
                             make_is_ancestor({'x': 'base', 'y': 'base'}))
    assert 'diverged' in plan['refusal']


def test_branch_plan_refuses_a_third_branch_and_a_detached_head():
    assert 'main or dev' in bless.branch_plan(
        'feature', tips('a', 'a'), make_is_ancestor({}))['refusal']
    assert 'detached' in bless.branch_plan(
        None, tips('a', 'a'), make_is_ancestor({}))['refusal']


def test_branch_plan_refuses_a_push_that_would_not_fast_forward():
    plan = bless.branch_plan(
        'main', tips('b', 'b', origin_main='elsewhere'),
        make_is_ancestor({'b': 'a'}))
    assert 'fast-forward' in plan['refusal']


def test_the_tool_floors_are_the_ones_pyproject_installs():
    dev = PROJECT['optional-dependencies']['dev']
    for tool, floor in bless.TOOL_FLOORS.items():
        wanted = '.'.join(str(part) for part in floor)
        assert any(requirement.startswith(f'{tool}>={wanted.split(".")[0]}')
                   for requirement in dev), (tool, dev)


def test_an_old_validator_is_refused_by_preflight(monkeypatch):
    monkeypatch.setattr(bless, 'installed_version', lambda name: '6.1.0')
    with pytest.raises(bless.BlessError, match='older'):
        bless._tool_floor('twine', (7, 0, 0))


def test_a_missing_tool_says_why_and_names_both_install_commands(monkeypatch):
    """flit can build, so the refusal has to say what twine is still doing."""

    def missing(name):
        raise bless.PackageNotFoundError(name)

    monkeypatch.setattr(bless, 'installed_version', missing)
    with pytest.raises(bless.BlessError) as caught:
        bless._tool_floor('twine', (7, 0, 0))

    message = str(caught.value)
    assert 'twine check --strict' in message
    assert 'pip install -e ".[dev]"' in message
    assert 'flit install --extras dev' in message


def test_the_post_upload_verification_compares_what_the_index_reports(monkeypatch, tmp_path):
    import hashlib

    wheel = tmp_path / 'w.whl'
    wheel.write_bytes(b'wheel-bytes')
    digest = hashlib.sha256(b'wheel-bytes').hexdigest()
    good = {
        'info': {
            'summary': PROJECT['description'],
            'requires_python': PROJECT['requires-python'],
            # The index also lists the dev extra; it must not be demanded of users.
            'requires_dist': [*PROJECT['dependencies'],
                              'pytest>=7; extra == "dev"'],
            'keywords': ', '.join(PROJECT['keywords']),
        },
        'releases': {VERSION: [
            {'packagetype': 'bdist_wheel', 'digests': {'sha256': digest}}]},
    }
    assert bless._metadata_mismatches(VERSION, wheel, good) == []

    bad = {
        'info': {
            'summary': 'A GTK tray applet for power management',
            'requires_python': '>=3.8',
            'requires_dist': ['pydbus>=0.6.0'],
            'keywords': 'power',
        },
        'releases': {VERSION: [
            {'packagetype': 'bdist_wheel', 'digests': {'sha256': 'nope'}}]},
    }
    joined = '; '.join(bless._metadata_mismatches(VERSION, wheel, bad))
    for field in ('SHA256', 'summary', 'requires_python', 'Requires-Dist', 'keywords'):
        assert field in joined, field


# --- the shared inspector ---------------------------------------------------

def make_wheel(path, *, summary=None, version=None, dependencies=None,
               include_tests=False, resources=True):
    metadata = [
        'Metadata-Version: 2.5',
        'Name: pwr_tray',
        f'Version: {version or VERSION}',
        f'Summary: {summary or PROJECT["description"]}',
        f'Requires-Python: {PROJECT["requires-python"]}',
        *[f'Requires-Dist: {dep}' for dep in
          (dependencies if dependencies is not None else PROJECT['dependencies'])],
        f'Keywords: {", ".join(PROJECT.get("keywords", []))}',
        '',
        'A description body.',
    ]
    names = []
    if resources:
        names += list(inspect_dist.REQUIRED_RESOURCES)
        names += [f'pwr_tray/resources/systemd/{unit.name}'
                  for unit in (ROOT / 'pwr_tray' / 'resources' / 'systemd').glob('*.service')]
    if include_tests:
        names.append('tests/test_metadata.py')
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('pwr_tray-2.0.0.dist-info/METADATA', '\n'.join(metadata))
        for name in names:
            archive.writestr(name, '')
    return path


def make_sdist(path, *, include_tests=True):
    names = ['pwr_tray-2.0.0/pyproject.toml',
             'pwr_tray-2.0.0/conftest.py',
             'pwr_tray-2.0.0/pwr_tray/resources/commands.yaml']
    if include_tests:
        names.append('pwr_tray-2.0.0/tests/test_metadata.py')
    with tarfile.open(path, 'w:gz') as archive:
        for name in names:
            info = tarfile.TarInfo(name)
            archive.addfile(info)
    return path


def test_a_correct_wheel_and_sdist_pass(tmp_path):
    inspect_dist.check_all(make_wheel(tmp_path / 'ok.whl'),
                           make_sdist(tmp_path / 'ok.tar.gz'))


def test_the_inspector_rejects_the_gtk_summary_the_index_serves(tmp_path):
    wheel = make_wheel(tmp_path / 'gtk.whl',
                       summary='A GTK tray applet for power management')
    with pytest.raises(inspect_dist.DistributionError) as caught:
        inspect_dist.check_wheel(wheel)
    assert 'Summary' in str(caught.value)


def test_the_inspector_rejects_pydbus_in_the_wheel_metadata(tmp_path):
    wheel = make_wheel(tmp_path / 'pydbus.whl',
                       dependencies=[*PROJECT['dependencies'], 'pydbus>=0.6.0'])
    with pytest.raises(inspect_dist.DistributionError) as caught:
        inspect_dist.check_wheel(wheel)
    assert 'Requires-Dist' in str(caught.value)


def test_the_inspector_keeps_wheel_contents_honest(tmp_path):
    with_tests = make_wheel(tmp_path / 'tests.whl', include_tests=True)
    with pytest.raises(inspect_dist.DistributionError, match='tests/'):
        inspect_dist.check_wheel(with_tests)

    bare = make_wheel(tmp_path / 'bare.whl', resources=False)
    with pytest.raises(inspect_dist.DistributionError, match='missing'):
        inspect_dist.check_wheel(bare)


def test_the_sdist_must_be_able_to_run_the_gate(tmp_path):
    with pytest.raises(inspect_dist.DistributionError, match='tests/'):
        inspect_dist.check_sdist(make_sdist(tmp_path / 'no-tests.tar.gz',
                                            include_tests=False))
