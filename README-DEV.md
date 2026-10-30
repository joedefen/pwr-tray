# pwr-tray: working on the source tree

Internal notes. The user-facing documentation is `README.md`; this file is
about developing, testing and releasing `pwr-tray`, and about the parts of the
service integration that only matter to someone editing the code.

## Layout and single sources of truth

| Fact | Where it lives | Do not |
|---|---|---|
| version | `pwr_tray/__init__.py:__version__` (flit reads it) | add `version =` to `pyproject.toml`; `tests/test_metadata.py` fails |
| console script | `pwr-tray = pwr_tray.main:main` in `pyproject.toml` | write a second entry point |
| DE commands | `pwr_tray/resources/commands.yaml` | edit the copy in `~/.config/pwr-tray/` as if it were source |
| systemd units | `pwr_tray/resources/systemd/pwr-tray-*.service` | hand-edit `~/.config/systemd/user/pwr-tray.service` and call it a fix |

## The service contract (and why it is this dumb)

`pwr-tray.service` **always** runs `%h/.local/bin/pwr-tray`. `--svc-install`
copies the shipped unit and fills in one machine fact -- the session's
`WAYLAND_DISPLAY`/`DISPLAY` -- and never records an installation path.

This is deliberate. Before it, `--svc-install` baked in the absolute path of
whichever `pwr-tray` `shutil.which()` found, which meant:

* installing from an activated project virtualenv recorded
  `<checkout>/.venv/bin/pwr-tray`;
* a plain `pip install .` in that virtualenv had copied the package into its
  `site-packages`, so that executable imported the **frozen copy**, not the
  checkout;
* the unit was "up to date", `--version` matched, and every `--svc-restart`
  reloaded the snapshot. Nothing in the output said why.

One path, chosen by the user's install method, removes the whole class of bug:

* `--svc-install`, `--svc-start`, `--svc-restart` refuse (exit 1, no
  `systemctl` call) when `~/.local/bin/pwr-tray` is missing, dangling or not
  executable -- such a unit can only fail;
* `--svc-status`, `--svc-stop`, `--svc-uninstall` never refuse; those are the
  verbs you need when the install is broken;
* a unit that runs anything else (hand edit, or one installed by an older
  pwr-tray) is refused by start/restart with "run `--svc-install`";
* `--svc-status` prints the launcher, its target, and what that target is.

Nothing about the effective executable is silent. `service.installation()`
reads pip's `direct_url.json` next to the installed `pwr_tray-*.dist-info` and
reports `editable`, `source`, and `snapshot` (a non-editable copy of a checkout
that still exists). `snapshot` is the only case worth a warning: restarting
that launcher reloads the copy forever. `--svc-install` and `--svc-status` say
so; `--svc-restart` says so instead of promising it applied your edits.

Consequence for you as a developer: **never make a non-editable install into
the environment `~/.local/bin/pwr-tray` points at.** In a checkout venv, use
`python -m pip install -e .`; with pipx, use `pipx install --editable .`.

## Development loop

```
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

* `.venv/bin/python -m pwr_tray.main -D -o` -- run the tree, log to stdout.
* `./runner [args]` -- the same, but `execve`s `python -m pwr_tray.main` with
  `--replace`, so it looks like a restarted instance to `ps`/`PyKill` and takes
  the single-instance lock from whatever is running. That includes the systemd
  unit: after `./runner`, the service's own start exits 3 and the unit goes
  `failed` (`RestartPreventExitStatus=2 3`). Either `--svc-stop` first, or stop
  using `./runner` and iterate with `--svc-restart`.
* `pwr-tray --svc-restart` applies the checkout to a systemd-managed applet
  when the launcher is the editable install.

Verifying which code a running process loaded:

```
pwr-tray --svc-status                    # launcher, target, editable or not
python -c "import pwr_tray; print(pwr_tray.__file__)"
pid=$(systemctl --user show pwr-tray.service -p MainPID --value)
cat /proc/$pid/maps | grep -m1 pwr_tray  # or: readlink /proc/$pid/exe
```

## Test gate

```
.venv/bin/python -m ruff check .
.venv/bin/python -m compileall -q pwr_tray
.venv/bin/python -m pytest -q --cov=pwr_tray --cov-report=term-missing
```

The last command is the gate, not an optional extra: it fails when the total
branch coverage drops below `[tool.coverage.report] fail_under` in
`pyproject.toml`, and CI runs exactly this command on every supported Python.
The floor lives in `pyproject.toml` only -- pytest-cov reads `fail_under`
itself -- so there is no second copy to drift.

At the time of writing: **76.3% total** (485 statements and 98 partial branches
missed, 2208 statements and 752 branches measured). The report is dull where the
tests are dense and pointed where they are not:

| Module | Cover |
| --- | ---: |
| `Errors.py` | 90.7% |
| `IniTool.py` | 93.5% |
| `service.py` | 92.8% |
| `Utils.py` | 73.7% |
| `PowerEvents.py` | 71.4% |
| `main.py` | 66.5% |
| `SwayIdleMgr.py` | 63.6% |

Nearly all of the misses are in `main.py` and are structural, not forgotten:
`PwrTray.__init__`'s menu construction, the `QTimer`/event-loop wiring, and
`swayidle` supervision need a live session. The action layer that *can* be
tested is pinned in `tests/test_actions.py`; `term-missing` is how you find the
desktop branch that has no expectation yet, which is the one thing a test count
cannot tell you.

`tests/conftest.py` redirects `HOME` to a temporary directory and stubs
PyQt5/ruamel.yaml/psutil when they are absent, so the suite runs in a bare
container as well as on a desktop and never touches your real `~/.config`.
The stubs do not move the coverage number: the suite measures the same 76.3%
with the real libraries and with the stubs.

Service integrations worth knowing, because they are policy rather than code:

* `tests/test_service_commands.py` -- the `--svc-*` verbs: what each one
  writes, in what order, what it refuses, and the missing-launcher cases.
* `tests/test_service_examples.py` -- the shipped units: that they parse, that
  `render()` leaves the launcher untouched, and that every `StandardOutput=`
  file target resolves to a directory `--svc-install` can create.
* `tests/test_service_provenance.py` -- editable vs frozen installs, read from
  real virtualenv-shaped directories.
* `tests/test_single_instance.py` -- command-line recognition, the lock-first
  start-up order, and `./runner`'s `--replace`.

## Releasing

`./bless` is the whole procedure.  It is a dry run by default and prints the
checklist it would execute; `./bless test` rehearses the upload on TestPyPI;
`./bless publish` performs the irreversible part.

1. bump `__version__` in `pwr_tray/__init__.py` (the only place), then commit.
2. `./bless` -- preflight (clean tree, tag absent, tool floors), the PyPI
   duplicate check, the same three gates CI runs, a build into
   `build/bless-<version>/`, inspection with `tests/inspect_dist.py`, `twine
   check --strict`, and a smoke install of the built wheel in a throwaway
   virtualenv.
3. `./bless test` -- the rehearsal.  It builds a throwaway `X.Y.Z.99.N` from a
   *copy* of the tree, where `N` is one past the highest suffix TestPyPI
   already holds for that base version (0 the first time), and uploads it only
   to `test.pypi.org`.  The tree keeps its real version, no tag is created, no
   branch moves, and the same gates and inspector run.  It then prints the
   install line, which needs `python -m pip cache purge` first -- pip remembers
   an index it has already read -- and `--extra-index-url
   https://pypi.org/simple/`, because TestPyPI carries none of the
   dependencies.  `TWINE_TESTPYPI_PASSWORD` is the TestPyPI token; a TestPyPI
   token is not a PyPI token, so both can be exported at once.
4. `./bless publish` -- fast-forward `main` and `dev` to one commit, tag
   `vX.Y.Z`, upload the two named files to PyPI, verify the index, and push.

The duplicate check runs in **every** mode, against the index that mode
targets, and before anything is built.  If the version in
`pwr_tray/__init__.py` is already published the command refuses and prints
every release that index has, because an upload cannot be replaced.  A 404
counts as "nothing published yet", which is what either index answers before a
project's first upload.  For `publish`, `TWINE_PASSWORD` must be set;
`TWINE_USERNAME` defaults to `__token__`.

### Why not a release workflow

Publishing is irreversible, and the owner runs it from the development tree, so
the safety lives in one inspectable command rather than in a CI job that can
only see what it just built.  CI stays a gate: it builds and inspects the same
way (`tests/inspect_dist.py`) but never holds an upload credential.  The
reasoning is recorded in `plans/pypi-release-plan.md` (R1/R2) so a later audit
does not re-file it as missing automation.
