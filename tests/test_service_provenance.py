"""What the launcher behind a unit really imports.

The unit is a constant: it runs ``~/.local/bin/pwr-tray`` and nothing else, so
installing one never records an installation and no shell can influence it.
The remaining question is what that launcher points at.  ``pip install .``
inside a project virtualenv leaves a *frozen* copy of the package in that
virtualenv's site-packages, and a launcher pointing there can never see edits
to the checkout -- the unit is up to date, ``--version`` matches, and the tray
on screen is old.

pip records the difference in ``direct_url.json`` next to the installed
metadata, so it is knowable: an editable install has ``dir_info.editable`` and
a URL back to the tree, a ``pip install .`` snapshot has a plain ``file://``
URL, and an index install has none.  These tests build real virtualenv-shaped
directories and point the launcher at them, rather than stubbing a lookup.
"""

import json

import pytest

from pwr_tray import service

DESKTOP = 'niri-wayland'


class FakeSystemctl:
    """Answers the state queries and records every call, in order."""

    QUERIES = ('is-enabled', 'is-active', 'is-failed')

    def __init__(self, enabled=True, active=False, failed=False):
        self.enabled = enabled
        self.active = active
        self.failed = failed
        self.calls = []

    def __call__(self, argv):
        argv = tuple(argv)
        self.calls.append(argv)
        assert argv[:2] == ('systemctl', '--user'), argv
        if argv[2] == 'is-enabled':
            return (0, 'enabled') if self.enabled else (1, 'disabled')
        if argv[2] == 'is-active':
            return (0, 'active') if self.active else (3, 'inactive')
        if argv[2] == 'is-failed':
            return (0, 'failed') if self.failed else (1, 'active')
        return 0, ''

    def mutating(self):
        return [argv for argv in self.calls if argv[2] not in self.QUERIES]


@pytest.fixture
def env(tmp_path):
    return {
        'HOME': str(tmp_path),
        'XDG_CONFIG_HOME': str(tmp_path),
        'XDG_RUNTIME_DIR': '/run/user/1000',
        'WAYLAND_DISPLAY': 'wayland-0',
        'DISPLAY': ':0',
        'PATH': '/usr/local/bin:/usr/bin:/bin',
    }


def make_checkout(tmp_path, editable, name='.venv'):
    """Build a source tree whose virtualenv holds an installed pwr_tray.

    Mirrors what ``pip install .`` and ``pip install -e .`` leave behind: the
    console script in ``<venv>/bin``, the metadata in
    ``<venv>/lib/pythonX.Y/site-packages``, and ``direct_url.json`` recording
    where the distribution came from and whether it is editable.
    """
    checkout = tmp_path / 'checkout'
    (checkout / 'pwr_tray').mkdir(parents=True)
    (checkout / 'pwr_tray' / 'main.py').write_text('', encoding='utf-8')
    (checkout / 'pyproject.toml').write_text('', encoding='utf-8')

    bindir = checkout / name / 'bin'
    bindir.mkdir(parents=True)
    executable = bindir / 'pwr-tray'
    executable.write_text('#!/usr/bin/python3\n', encoding='utf-8')
    executable.chmod(0o755)

    dist = (checkout / name / 'lib' / 'python3.13' / 'site-packages'
            / 'pwr_tray-2.0.0.dist-info')
    dist.mkdir(parents=True)
    direct = {'url': checkout.as_uri()}
    if editable:
        direct['dir_info'] = {'editable': True}
    (dist / 'direct_url.json').write_text(json.dumps(direct), encoding='utf-8')
    return executable, checkout


def point_launcher_at(env, target):
    """Make ``~/.local/bin/pwr-tray`` a symlink to `target`, pipx-style."""
    bindir = service.launcher_path(env).parent
    bindir.mkdir(parents=True, exist_ok=True)
    link = bindir / 'pwr-tray'
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)
    return link


def run_verb(verb, env, run, **kwargs):
    kwargs.setdefault('echo', lambda line: None)
    kwargs.setdefault('holder', None)
    kwargs.setdefault('systemd', True)
    return service.dispatch(verb, DESKTOP, env=env, run=run, **kwargs)


# --------------------------------------------------------------------------
# reading the provenance
# --------------------------------------------------------------------------

def test_a_non_editable_install_of_a_live_checkout_is_a_snapshot(tmp_path):
    executable, checkout = make_checkout(tmp_path, editable=False)
    info = service.installation(executable)

    assert info.snapshot is True
    assert info.editable is False
    assert info.source == checkout
    assert info.environment == checkout / '.venv'


def test_an_editable_install_is_not_a_snapshot(tmp_path):
    executable, checkout = make_checkout(tmp_path, editable=True)
    info = service.installation(executable)

    assert info.editable is True
    assert info.snapshot is False
    assert info.source == checkout


def test_a_checkout_that_no_longer_exists_is_not_a_snapshot(tmp_path):
    """A release installed from a tree that is gone cannot shadow anything."""
    executable, checkout = make_checkout(tmp_path, editable=False)
    (checkout / 'pyproject.toml').unlink()

    assert service.installation(executable).snapshot is False


def test_a_wheel_from_an_index_names_no_source(tmp_path):
    executable, _ = make_checkout(tmp_path, editable=False)
    metadata = executable.parents[1] / 'lib'
    for direct in metadata.glob('python*/site-packages/*/direct_url.json'):
        direct.unlink()

    info = service.installation(executable)
    assert (info.editable, info.snapshot, info.source) == (False, False, None)


def test_an_unknown_executable_is_reported_honestly(tmp_path):
    info = service.installation(tmp_path / 'nowhere' / 'bin' / 'pwr-tray')

    assert (info.editable, info.snapshot, info.source) == (False, False, None)


def test_the_launcher_resolves_to_the_install_it_points_at(tmp_path):
    """~/.local/bin/pwr-tray is a symlink into a venv, not an install."""
    env = {'HOME': str(tmp_path)}
    executable, checkout = make_checkout(tmp_path, editable=True, name='venvs/pwr-tray')
    point_launcher_at(env, executable)

    info = service.launcher_installation(env)
    assert info.executable == executable
    assert info.environment == checkout / 'venvs' / 'pwr-tray'
    assert info.snapshot is False


# --------------------------------------------------------------------------
# what the verbs say about it
# --------------------------------------------------------------------------

def test_install_reports_an_editable_launcher(env, tmp_path):
    executable, checkout = make_checkout(tmp_path, editable=True, name='venvs/pwr-tray')
    launcher = point_launcher_at(env, executable)

    output = []
    assert run_verb('install', env, FakeSystemctl(), echo=output.append) == service.OK

    joined = '\n'.join(output)
    assert f'the unit will run {launcher}' in joined
    assert f'is an editable install of {checkout}' in joined
    assert 'frozen copy' not in joined


def test_install_warns_when_the_launcher_is_a_frozen_copy(env, tmp_path):
    executable, checkout = make_checkout(tmp_path, editable=False)
    point_launcher_at(env, executable)

    output = []
    assert run_verb('install', env, FakeSystemctl(), echo=output.append) == service.OK

    joined = '\n'.join(output)
    assert 'frozen copy' in joined
    assert str(checkout) in joined


def test_restart_of_a_frozen_launcher_does_not_promise_new_code(env, tmp_path):
    executable, _ = make_checkout(tmp_path, editable=False)
    point_launcher_at(env, executable)
    run_verb('install', env, FakeSystemctl())

    output = []
    run = FakeSystemctl(enabled=True, active=True)
    assert run_verb('restart', env, run, echo=output.append) == service.OK

    assert run.mutating() == [('systemctl', '--user', 'restart', service.UNIT_NAME)]
    joined = '\n'.join(output)
    assert 'frozen copy' in joined
    assert 'resolves to' not in joined


def test_status_reports_where_the_launcher_points(env, tmp_path):
    executable, checkout = make_checkout(tmp_path, editable=True, name='venvs/pwr-tray')
    launcher = point_launcher_at(env, executable)
    run_verb('install', env, FakeSystemctl())

    output = []
    assert run_verb('status', env, FakeSystemctl(enabled=True, active=True),
                    echo=output.append) == service.STATUS_ACTIVE

    joined = '\n'.join(output)
    assert f'runs: {launcher}' in joined
    assert f'-> {executable}' in joined
    assert f'(editable install of {checkout})' in joined


def test_status_explains_a_frozen_launcher(env, tmp_path):
    executable, checkout = make_checkout(tmp_path, editable=False)
    point_launcher_at(env, executable)
    run_verb('install', env, FakeSystemctl())

    output = []
    run_verb('status', env, FakeSystemctl(enabled=True), echo=output.append)

    joined = '\n'.join(output)
    assert 'frozen copy' in joined
    assert str(checkout) in joined
