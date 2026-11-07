"""The half of the release gate that belongs to *this* project.

`bless` itself is tested once, in the repository that emits it
([`../../blessd/tests/test_bless.py`](../../blessd/tests/test_bless.py)): the
branch plan, the refusals, the prompt, the index check and the emitter are the
same wherever the gate is vendored.  What is not the same is the identity it
derives from this tree -- read at start-up, never written into the tool -- and
the inspector it runs before an upload, [`inspect_dist.py`](inspect_dist.py),
which asserts this project's metadata.  Those two are pinned here.
"""

import importlib.machinery
import importlib.util
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = 'pwr_tray'


def load_module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


bless = load_module('bless_gate', ROOT / 'bless')
inspect_dist = load_module('inspect_dist', ROOT / 'tests' / 'inspect_dist.py')

PROJECT = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']
VERSION = next(line.split('"')[1] for line in
               (ROOT / PACKAGE / '__init__.py').read_text(encoding='utf-8').splitlines()
               if line.startswith('__version__'))


# --- the identity the vendored gate derives from this tree -----------------


def test_this_tree_derives_its_own_identity():
    name = PROJECT['name']

    assert bless.PROJECT['name'] == name
    assert bless.PROJECT['module'] == PACKAGE
    assert bless.PROJECT['console'] == 'pwr-tray'
    assert bless.PROJECT['console'] in list(PROJECT['scripts'])
    assert bless.PROJECT['inspector'] == 'tests/inspect_dist.py'
    # No [tool.bless] branches in pyproject.toml, so the gate's default model
    # applies: the trunk and the development branch.
    assert bless.PROJECT['branches'] == ('main', 'dev')
    assert f'https://pypi.org/pypi/{name}/json' == bless.PYPI_URL
    assert f'https://test.pypi.org/pypi/{name}/json' == bless.TESTPYPI_URL
    assert bless.read_version() == VERSION


def test_the_version_the_gate_tags_is_the_package_version():
    import pwr_tray

    assert bless.read_version() == pwr_tray.__version__ == VERSION


# --- the shared inspector --------------------------------------------------


def test_the_inspector_reads_this_trees_own_identity():
    assert inspect_dist.project()['name'] == PROJECT['name']
    assert inspect_dist.expected_version() == VERSION


def make_wheel(path, *, summary=None, version=None, dependencies=None,
               include_tests=False, resources=True):
    metadata = [
        'Metadata-Version: 2.5',
        'Name: pwr_tray',
        f'Version: {version or VERSION}',
        f'Summary: {summary or PROJECT["description"]}',
        f'Requires-Python: {PROJECT["requires-python"]}',
        *[f'Requires-Dist: {dependency}' for dependency in
          (dependencies if dependencies is not None else PROJECT['dependencies'])],
        f'Keywords: {", ".join(PROJECT.get("keywords", []))}',
        '',
        'A description body.',
    ]
    names = []
    if resources:
        names += list(inspect_dist.REQUIRED_RESOURCES)
        names += [f'pwr_tray/resources/systemd/{unit.name}'
                  for unit in (ROOT / PACKAGE / 'resources' / 'systemd').glob('*.service')]
    if include_tests:
        names.append('tests/test_metadata.py')
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(f'pwr_tray-{version or VERSION}.dist-info/METADATA',
                         '\n'.join(metadata))
        for name in names:
            archive.writestr(name, '')
    return path


def make_sdist(path, *, include_tests=True):
    base = f'{PACKAGE}-{VERSION}'
    names = [f'{base}/pyproject.toml',
             f'{base}/conftest.py',
             f'{base}/{PACKAGE}/resources/commands.yaml']
    if include_tests:
        names.append(f'{base}/tests/test_metadata.py')
    with tarfile.open(path, 'w:gz') as archive:
        for name in names:
            archive.addfile(tarfile.TarInfo(name))
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
