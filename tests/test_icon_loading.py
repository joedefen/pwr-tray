"""Icon loading must not depend on the process working directory.

The tray icons were once located with relative paths that only resolved
because ``IniTool.__init__`` chdir()ed into ~/.config/pwr-tray.  A copy that
failed then left ``self.icons`` short and ``show_icon()`` raised
IndexError on the next state change.  These tests pin the replacement:
resources are resolved against an explicit folder, and anything that cannot
be installed is a startup error.
"""

import ast
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pwr_tray import Utils  # noqa: E402
from pwr_tray import main as main_module  # noqa: E402  (after sys.path setup)
from pwr_tray.Errors import MissingResourcesError  # noqa: E402
from pwr_tray.IniTool import IniTool  # noqa: E402

_SVG_INFO = main_module.PwrTray.svg_info
SVGS = [f'{base}-v{_SVG_INFO.version}.svg' for base in _SVG_INFO.bases]


def test_ensure_resources_installs_into_the_given_folder(tmp_path):
    """Missing files are copied in and returned as absolute paths."""
    folder = tmp_path / 'config'
    folder.mkdir()
    paths = main_module.PwrTray.ensure_resources(str(folder), SVGS)

    assert len(paths) == len(SVGS)
    for path, resource in zip(paths, SVGS, strict=True):
        assert path == os.path.join(str(folder), resource)
        assert os.path.isabs(path)
        assert os.path.isfile(path)


def test_ensure_resources_ignores_the_working_directory(tmp_path, monkeypatch):
    """A different cwd must not alter where the files are looked up."""
    folder = tmp_path / 'config'
    folder.mkdir()
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    paths = main_module.PwrTray.ensure_resources(str(folder), SVGS[:1])

    assert paths[0].startswith(str(folder))
    assert os.path.isfile(paths[0])
    assert os.listdir(elsewhere) == []


def test_missing_resource_aborts_with_the_folder_in_the_message(tmp_path,
                                                               monkeypatch):
    """A file that cannot be installed is fatal, not a smaller icon list."""
    folder = tmp_path / 'config'
    folder.mkdir()
    monkeypatch.setattr(Utils, 'copy_to_folder', lambda name, dest: None)

    with pytest.raises(MissingResourcesError) as excinfo:
        main_module.PwrTray.ensure_resources(str(folder), ['no-such-icon.svg'])

    message = str(excinfo.value)
    assert 'no-such-icon.svg' in message
    assert str(folder) in message


def test_failed_copy_is_reported_as_a_missing_resource(tmp_path, monkeypatch):
    """A copy that raises (read-only folder) becomes the same clear error."""
    folder = tmp_path / 'config'
    folder.mkdir()

    def failing_copy(name, dest):
        raise OSError('read-only file system')

    monkeypatch.setattr(Utils, 'copy_to_folder', failing_copy)

    with pytest.raises(MissingResourcesError) as excinfo:
        main_module.PwrTray.ensure_resources(str(folder), SVGS)

    assert 'Unlocked-v03.svg' in str(excinfo.value)


def test_init_passes_the_config_folder_to_ensure_resources():
    """Regression: ``__init__`` called ``ensure_resources(self.svgs)``.

    The signature is ``(folder, resources)``, so that one-argument call raised
    ``TypeError`` only after the single-instance lock had been taken -- the
    applet (and therefore ``./runner``) died on start-up with no tray icon.
    The call sites are outside the unit-testable part of the class, so the
    arity is checked statically instead of being left to a manual launch.
    """
    with open(os.path.join(ROOT, 'pwr_tray', 'main.py'),
              encoding='utf-8') as handle:
        source = handle.read()
    calls = [node for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)
             and node.func.attr == 'ensure_resources']

    assert calls, 'no ensure_resources() call sites found in main.py'
    for call in calls:
        rendered = ast.unparse(call)
        assert not call.keywords, rendered
        assert len(call.args) == 2, rendered          # folder, resources
        assert ast.unparse(call.args[0]) == 'ini_tool.folder', rendered


def test_init_does_not_change_the_working_directory():
    """IniTool must configure itself without moving the whole process."""
    start = os.getcwd()

    tool = IniTool()

    assert os.getcwd() == start
    assert os.path.isdir(tool.folder)
    assert os.path.isfile(tool.ini_path)
