"""Inspect the built wheel and sdist against this tree's own metadata.

One implementation, two callers: the packaging job in
``.github/workflows/tests.yml`` and ``./bless`` (which must never upload a
distribution it has not inspected).  Keeping the assertions here means the
command a maintainer runs and the command CI runs cannot drift apart.

The expected values are read from ``pyproject.toml`` and
``pwr_tray/__init__.py`` -- never from the artifact under test, which would
make every check tautological.

Run directly to inspect a built pair:

    python tests/inspect_dist.py dist/pwr_tray-2.0.0-py3-none-any.whl \\
                                dist/pwr_tray-2.0.0.tar.gz
"""

from __future__ import annotations

import re
import sys
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# This module is run as a script by CI (`python tests/inspect_dist.py`), so the
# repository root -- not tests/ -- must be importable before `import pwr_tray`.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# What the wheel must contain besides metadata: the data the applet reads at
# run time and the units `--svc-install` copies out.  A wheel without them
# installs but cannot run, which the metadata alone would not reveal.
REQUIRED_RESOURCES = (
    'pwr_tray/resources/commands.yaml',
    'pwr_tray/resources/lockpaper.png',
    'pwr_tray/resources/FullSun-v03.svg',
)


class DistributionError(ValueError):
    """One or more assertions failed; the message lists all of them."""


def project() -> dict:
    """The `[project]` table of this tree's pyproject.toml."""
    with open(ROOT / 'pyproject.toml', 'rb') as handle:
        return tomllib.load(handle)['project']


def expected_version() -> str:
    """The single source of truth, not the artifact's own claim."""
    import pwr_tray

    return pwr_tray.__version__


def _requirement_name(requirement: str) -> str:
    match = re.match(r'[A-Za-z0-9_.-]+', requirement.strip())
    return (match.group(0) if match else requirement).lower()


def runtime_names(requirements) -> set[str]:
    """Names of the *runtime* requirements, ignoring the `dev` extra.

    The build backend writes every extra into `Requires-Dist` with an
    `extra == "..."` marker, so an unfiltered comparison would demand that a
    user install pytest and ruff to run the applet.
    """
    return {_requirement_name(requirement) for requirement in requirements
            if 'extra ==' not in requirement}


def _keyword_set(value: str | None) -> set[str]:
    if not value:
        return set()
    return {part.strip() for part in value.split(',') if part.strip()}


def wheel_metadata(wheel: Path) -> dict:
    """Parse METADATA out of a built wheel.

    Returns the fields this project asserts on, with `Requires-Dist` as a list
    and `Keywords` as a set, so callers do not repeat the email parsing.
    """
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata_name = next(
            (name for name in names if name.endswith('.dist-info/METADATA')), None)
        if metadata_name is None:
            raise DistributionError(f'{wheel}: no .dist-info/METADATA')
        message = Parser().parsestr(archive.read(metadata_name).decode('utf-8'))

    return {
        'Name': message['Name'],
        'Version': message['Version'],
        'Summary': message['Summary'],
        'Requires-Python': message['Requires-Python'],
        'Requires-Dist': message.get_all('Requires-Dist') or [],
        'Keywords': _keyword_set(message['Keywords']),
    }


def check_wheel(wheel: Path, project_table: dict | None = None,
                version: str | None = None) -> None:
    """Assert the wheel's metadata and contents match this tree.

    Raises `DistributionError` naming every failure, rather than the first, so
    a bad build is fixed in one pass.  `version` overrides
    `expected_version()`, which is what `./bless test` needs: it builds a
    throwaway `X.Y.Z.99.N` on purpose, and that one field is the whole
    difference to assert against.
    """
    project_table = project_table or project()
    expected = {
        'Name': project_table['name'],
        'Version': version or expected_version(),
        'Summary': project_table['description'],
        'Requires-Python': project_table['requires-python'],
    }
    problems: list[str] = []

    metadata = wheel_metadata(Path(wheel))
    for field, wanted in expected.items():
        found = metadata[field]
        if found != wanted:
            problems.append(f'{field}: expected {wanted!r}, found {found!r}')

    declared = runtime_names(project_table['dependencies'])
    built = runtime_names(metadata['Requires-Dist'])
    if built != declared:
        problems.append(
            'Requires-Dist: expected exactly '
            f'{sorted(declared)}, found {sorted(built)}')

    if metadata['Keywords'] != set(project_table.get('keywords', [])):
        problems.append(
            'Keywords: expected '
            f'{sorted(project_table.get("keywords", []))}, '
            f'found {sorted(metadata["Keywords"])}')

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()

    for resource in REQUIRED_RESOURCES:
        if resource not in names:
            problems.append(f'missing from the wheel: {resource}')

    units = sorted((ROOT / 'pwr_tray' / 'resources' / 'systemd').glob('*.service'))
    if not units:
        problems.append('no systemd units found in the tree to compare against')
    for unit in units:
        wanted = f'pwr_tray/resources/systemd/{unit.name}'
        if wanted not in names:
            problems.append(f'missing from the wheel: {wanted}')

    if any(name.startswith('tests/') for name in names):
        problems.append('the wheel contains tests/, which it must not')
    if any('.venv' in name or '__pycache__' in name for name in names):
        problems.append('the wheel contains a virtualenv or bytecode cache')

    if problems:
        raise DistributionError(
            f'{wheel}: ' + '; '.join(problems))


def check_sdist(sdist: Path) -> None:
    """Assert the sdist can run its own gate and carries the resources."""
    with tarfile.open(sdist) as archive:
        names = archive.getnames()

    problems: list[str] = []
    for suffix in ('pwr_tray/resources/commands.yaml',
                   'pyproject.toml',
                   'conftest.py',
                   'tests/test_metadata.py'):
        if not any(name.endswith(suffix) for name in names):
            problems.append(f'missing from the sdist: {suffix}')
    if any('.venv' in name or '__pycache__' in name for name in names):
        problems.append('the sdist contains a virtualenv or bytecode cache')

    if problems:
        raise DistributionError(f'{sdist}: ' + '; '.join(problems))


def check_all(wheel: Path, sdist: Path, version: str | None = None) -> None:
    check_wheel(Path(wheel), version=version)
    check_sdist(Path(sdist))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        print('usage: inspect_dist.py <wheel> <sdist>', file=sys.stderr)
        return 2
    wheel, sdist = argv
    try:
        check_all(Path(wheel), Path(sdist))
    except DistributionError as exc:
        print(f'inspect_dist: {exc}', file=sys.stderr)
        return 1
    print(f'distributions OK: {wheel} and {sdist}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
