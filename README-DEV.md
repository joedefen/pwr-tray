# pwr-tray: working on the source tree

Internal notes. The user-facing documentation is `README.md`; this file is
about developing, testing and releasing `pwr-tray`, and about the parts of the
service integration that only matter to someone editing the code.

## Project overview

`pwr-tray` is a PyQt5 tray applet that owns idle policy -- lock, then optionally
sleep or power off -- shows *why* the machine is inhibited, and can inhibit
locking or sleeping on request. Policy is per battery tier; behaviour is per
desktop, chosen from a YAML table.

The subsections below are the shape of the program: what the pieces are, what
happens at start-up, what the event loop actually does on a tick, and how
`config.ini` reaches it. Everything later in this file assumes them.

### Modules

| File | Responsibility |
| --- | --- |
| `pwr_tray/main.py` | Composition root and facade: Qt timer and tray wiring, dirty-flag scheduler, menu, icons, event handlers, `main()`, and thin delegates to the modules below |
| `pwr_tray/IdleStateMachine.py` | The idle branch ladder -- lock, blank, suspend, power off, reset, awake -- plus the idle-time and state primitives |
| `pwr_tray/Actions.py` | The user-action layer: every menu entry that starts a process, with the shell semantics of `commands.yaml` |
| `pwr_tray/Desktop.py` | DE detection, command validation, variable expansion and resource installation over `commands.yaml` |
| `pwr_tray/PowerEvents.py` | Event sources: two long-lived `dbus-monitor` children (system bus, session bus) plus a config-file watcher |
| `pwr_tray/TrayWatch.py` | Watches the tray host's `org.kde.StatusNotifierWatcher` bus name so the applet re-registers its tray item when a bar starts late or restarts |
| `pwr_tray/IniTool.py` | `config.ini` parsing, tier inheritance, defaults, list parsing, change detection |
| `pwr_tray/Utils.py` | Console capture, log rotation, logging, process discovery and killing, the `flock` single-instance lock |
| `pwr_tray/SwayIdleMgr.py` | Builds and supervises the `swayidle` command line on Wayland |
| `pwr_tray/Errors.py` | Three user-facing exceptions with actionable messages |
| `pwr_tray/service.py` | The `--svc-*` verbs: desktop-to-unit mapping, install-time rendering, log-directory creation, status probe, plan/execute split |
| `pwr_tray/resources/` | `commands.yaml`, the icons installed at first run, and the ten systemd units `--svc-install` copies out |

The arrows point one way: `main.py` knows about everything, and nothing below it
imports `main.py`. That is what lets the collaborators be driven without a Qt
session, and it is worth keeping if you are tempted to move a helper "up" for
convenience.

### How a run starts

`main()` parses arguments, short-circuits the non-GUI flags (`--list-des`,
`--edit-config`, `--follow-log`), then:

1. constructs `IniTool`, which creates `~/.config/pwr-tray/config.ini` and
   copies the resources if they are missing;
2. registers the log path and calls `Utils.capture_console()` *before* the first
   `prt()`, because `prt()` may redirect fd 2 into `debug.log` and would
   otherwise swallow the startup error that matters;
3. takes the `flock` through `acquire_lock_or_take_over()` and registers
   `atexit` handlers for the lock and for `PwrTray.goodbye`;
4. builds `PwrTray`, where desktop detection and the resource and command checks
   can fail with a typed error, reported to both the log and the console, and
   exiting 2;
5. enters the Qt event loop.

Nothing waits for the bar. `PwrTray` asks once whether a
`StatusNotifierWatcher` is registered (for the log), shows its tray item
immediately, and starts a `TrayWatch`; a watcher that appears later -- a bar
still starting, or one restarted -- is what makes the item be registered again.
An X11 bar with no watcher at all is not an error: the icon is simply not
visible there.

The lock is taken **before** anything destructive, and it buys exactly one
thing: the right to replace an instance. That path is opt-in via `--replace` and
re-verifies the recorded pid's command line before signalling it
(`PyKill.kill_pid`).

### Runtime model

* **One timer, recomputed.** A single `QTimer` is re-armed at whatever interval
  the current state needs; `poll_100ms` is how code says "soon" after something
  changed (100 ms is the fast cadence, not a fixed tick).
* **The tick is cheap.** `on_timeout()` only acts on dirty flags --
  `_inhibit_dirty`, `_battery_dirty`, `_config_dirty`. When an event source is
  unavailable for an input, that input is marked dirty every tick, so the code
  degrades to the old polling behaviour *per input* rather than globally.
* **The slow branch is the policy.** Every `loop_sample` ticks (1 instead of 15
  under `--quick`) it reads the idle time and asks `IdleStateMachine` which
  branch is due -- `LockOnly`, `SleepAfterLock`, `Presentation`, `NoSleep` --
  with `power_down` selecting `poweroff()` over `suspend()`.
* **Event sources.** `PowerEvents` watches logind inhibitor properties and
  `PrepareForSleep`, UPower AC/device changes, MPRIS playback status, and the
  config file. Monitors are drained through `QSocketNotifier`s, lines are
  classified by object path (`_context_for`), and the whole thing is stopped
  idempotently on exit.
* **Tray registration.** Qt registers the `QSystemTrayIcon` with
  `org.kde.StatusNotifierWatcher` once, when the item is shown, and never
  retries -- which is why an applet started before the bar used to stay
  invisible until it was restarted. `TrayWatch` reports the watcher's name
  being registered (or unregistered); `_install_tray_icon()` then hides the
  old item and creates a new one, which is what makes Qt register with the host
  that is running now. The five-minute resync retries the same thing in case a
  name event was missed.
* **Inhibition.** `check_inhibited()` asks logind for `BlockInhibited` (via
  `busctl`) and reads `playerctl status`; a blocked `sleep` or `idle` wins over
  media playback, the `systemd-inhibit` listing supplies the who/why rows, and
  the kind of inhibition decides the icon and whether the `swayidle` command
  keeps its timeouts.

### Configuration model

`[Settings]` supplies values to `[HiBattery]`/`[LoBattery]`, and an explicit
per-tier key overrides the inherited one (`_tier_base`, `_apply_section`). Lists
are validated by `to_array()`; malformed input falls back to the default instead
of raising. The rotate state is not configuration and is not stored with it --
the user's picks are persisted separately in `picks.json`, and restored on
independent axes.

### Tests, packaging and CI

The suite is split by concern, not by source file: `tests/test_actions.py` pins
the command each desktop is given, `test_idle_state_machine.py` drives the
branch ladder, `test_media_inhibit.py` the inhibition verdict, `test_power_events.py`
event classification and dirty-flag coalescing, and the `test_service_*` modules
the `--svc-*` verbs and the shipped units. How to run it, and the coverage floor
that makes it a gate rather than a habit, are in [Test gate](#test-gate).

Packaging is flit: `pyproject.toml` declares Python >= 3.11 and three runtime
dependencies (`psutil`, `PyQt5`, `ruamel.yaml`), and the resources directory
rides in the sdist. CI runs ruff, `compileall` and pytest on 3.11/3.12/3.13,
then builds sdist+wheel, asserts the wheel's metadata and contents, installs the
wheel into a clean virtualenv, and runs `--version` and `--list-des` from the
installed console script -- so packaging is asserted, not assumed.

---

## Layout and single sources of truth

| Fact | Where it lives | Do not |
|---|---|---|
| version | `pwr_tray/__init__.py:__version__` (flit reads it) | add `version =` to `pyproject.toml`; `tests/test_metadata.py` fails |
| console script | `pwr-tray = pwr_tray.main:main` in `pyproject.toml` | write a second entry point |
| DE commands | `pwr_tray/resources/commands.yaml` | edit the copy in `~/.config/pwr-tray/` as if it were source |
| systemd units | `pwr_tray/resources/systemd/pwr-tray-*.service` | hand-edit `~/.config/systemd/user/pwr-tray.service` and call it a fix |

---

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

### The units are validated, not field-tested

`tests/test_service_examples.py` validates the shipped units: that they parse,
that the directives which matter are present, that every `--de` key exists in
`commands.yaml`, and that `render()` substitutes only the two machine values and
leaves the launcher alone. Because the units ride in the wheel and
`--svc-install` writes them out from there, the file the tests validate and the
file a user runs are the same bytes -- which is the most a test can say from
here. Correctness against a live session manager, tray host and idle daemon is a
manual, per-desktop step; it is not asserted anywhere and should not be claimed
to be.

Two facts came out of the first field test and are now policy rather than
folklore:

* **systemd opens the log file but never creates its folder.** `--svc-install`
  reads the redirect paths back out of the unit it is about to write
  (`service.output_dirs()`/`missing_dirs()`) and creates them, announcing
  `create /home/...` in the plan. A unit installed by hand needs the same folder
  or `ExecStart` dies with `status=209/STDOUT`.
* **a unit that is already `failed` stays failed.** `--svc-start` and
  `--svc-restart` clear it with `systemctl --user reset-failed` before
  starting, so a start-rate limit is not mistaken for a broken applet.

---

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

---

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

---

## Releasing

`./bless` is the whole procedure, and it takes no arguments.  It runs every gate
on the tree, prints the checklist of what it now has in hand, and then asks

    ====> Enter test or publish or quit:

`test` rehearses the upload on TestPyPI, `publish` performs the irreversible
part, and Enter, `quit` or Ctrl-C stops with nothing tagged, uploaded or pushed.
The word has to be typed in full and anything else is asked again, because this
is the only question in the gate whose answer cannot be taken back.

1. bump `__version__` in `pwr_tray/__init__.py` (the only place), then commit.
2. `./bless` -- preflight (clean tree, tag absent, tool floors), the PyPI
   duplicate check, the same three gates CI runs, a build into
   `build/bless-<version>/`, inspection with `tests/inspect_dist.py`, `twine
   check --strict`, and a smoke install of the built wheel in a throwaway
   virtualenv.  Then the checklist, and the prompt.
3. answer `test` -- the rehearsal.  The gate has already run, so this adds only
   the upload path: it builds a throwaway `X.Y.Z.99.N` from a *copy* of the
   tree, where `N` is one past the highest suffix TestPyPI already holds for
   that base version (0 the first time), and uploads it only to
   `test.pypi.org`.  The tree keeps its real version, no tag is created, no
   branch moves, and the inspector and smoke install run on the copy too.  It
   then prints the
   install line, which needs `python -m pip cache purge` first -- pip remembers
   an index it has already read -- and `--extra-index-url
   https://pypi.org/simple/`, because TestPyPI carries none of the
   dependencies.  The credential is the TestPyPI one -- either
   `TWINE_TESTPYPI_PASSWORD` or the `~/.pypirc` section whose `repository` is
   `https://test.pypi.org/legacy/` -- because a TestPyPI token is not a PyPI
   token, and bless hands twine exactly the token for the index it is writing
   to.
4. answer `publish` -- fast-forward `main` and `dev` to one commit, tag
   `vX.Y.Z`, upload the two named files to PyPI, verify the index, and push.

The branch rule is part of releasing, not housekeeping.  `main` is where the
project lives and `dev` exists only for an overhaul like 2.0.0, so a release has
to leave `main` and `dev` at the same commit, and that commit tagged.  `publish`
establishes that before it uploads anything: it fast-forwards whichever branch
is behind, and the checklist names the ref it would move, so the decision is
visible before the question is asked.  Two branches that have diverged are
refused with both tips printed, as is a push that would not be a fast-forward on
`origin`.  Moving a local ref is free, instant and reversible; only the pushes
at the end touch the network, and each is a fast-forward of a ref that already
existed.

Verifying means reading the index back, and that read is patient on purpose.
Both indexes serve their JSON through a CDN that has lately taken minutes --
sometimes a quarter of an hour -- to list a newly accepted release. So the read
is cache-busted (a unique query string, because re-reading the URL the
pre-flight check just cached would spend the wait looking at the old answer) and
it polls for up to fifteen minutes, printing how long it has waited and what is
still missing. A release that is not listed yet, or is listed while its `info`
block still describes the previous one, is reported as lag, with the project
page to look at and the one step left for the answer given -- never as a failed
upload, because an accepted upload cannot be withdrawn.

The duplicate check runs on every pass of the gate, against PyPI and before
anything is built -- and once more against TestPyPI for the throwaway version,
before the rehearsal sends it.  If the version in
`pwr_tray/__init__.py` is already published the command refuses and prints
every release that index has, because an upload cannot be replaced.  A 404
counts as "nothing published yet", which is what either index answers before a
project's first upload.

Credentials are resolved per index: the environment variable wins
(`TWINE_PASSWORD` for PyPI, `TWINE_TESTPYPI_PASSWORD` for TestPyPI), otherwise
the `~/.pypirc` section whose `repository` matches that index's upload URL
(`https://upload.pypi.org/legacy/`, `https://test.pypi.org/legacy/`) supplies
the token.  Names of the sections do not matter; `TWINE_USERNAME` defaults to
`__token__`.  If neither source has a credential, bless refuses before twine
starts and shows both ways of providing one.

### Reusing `bless` in another project

`bless` holds no copy of this project's identity: it reads `[project] name`,
the package holding `__version__` (that name with `-` and `.` turned into `_`),
the console script (the first entry of `[project.scripts]`) and `[tool.bless]`
from `pyproject.toml` at start-up.  Copy the file to another project's root and
it works there, provided the project:

* keeps a flat layout -- the package directory at the root, since the script
  takes its root from its own path;
* declares `__version__ = "X.Y.Z"` in `<package>/__init__.py`, which the build
  backend reads (flit does), and never needs writing anywhere else;
* builds with `python -m build`, and has a dev extra with ruff, pytest,
  pytest-cov, build, twine>=7 and packaging>=26 -- `gates()` runs exactly
  ruff, compileall and `pytest --cov=<package>`.  twine is not a duplicate of
  flit: `flit publish` rebuilds and uploads what it just built, while `bless`
  uploads the two files it inspected, to the index the answer targets, after
  `twine check --strict` has passed on them;
* ships an inspector at `tests/inspect_dist.py` (or names one with
  `[tool.bless] inspect = "path"`), because bless refuses to upload what it has
  not inspected; `inspect = ""` opts out knowingly;
* if it declares a console script, has it print the version for `--version`;
* keeps the `main`/`dev` pair and tags `vX.Y.Z`.  The gate runs before the
  prompt, so it refuses a detached HEAD and an already-existing tag; a
  rehearsal is therefore always preceded by the checks the real release needs.

The rules are stated at the top of the script itself, so they travel with it.

### Why not a release workflow

Publishing is irreversible, and the owner runs it from the development tree, so
the safety lives in one inspectable command rather than in a CI job that can
only see what it just built.  CI stays a gate: it builds and inspects the same
way (`tests/inspect_dist.py`) but never holds an upload credential, and the
rehearsal on TestPyPI is optional and throwaway rather than a staging
dependency.  This paragraph is the record of that decision, so a later audit
does not re-file it as missing automation.
