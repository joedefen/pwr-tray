# pwr-tray — audit

**Package:** `pwr_tray` **2.0.0**
**Tree audited:** working tree at `dd0011f`, 2026-10-30
**Scope:** the whole repository — application modules, tests, packaging, CI, shipped docs/systemd units.
**Question asked:** is this sound, and does it have value relative to the alternatives?

This document describes the code as it stands. Nothing here is a changelog.

---

## 1. Verdict

Sound, deliberately engineered, and small enough to keep sound. The suite is real
(416 tests, 2.6 s with coverage) and the lint gate is clean; the architecture
separates event detection from policy and both from presentation. Nothing found
is a use-it-and-lose-data bug.

What was genuinely unfinished fell into two groups:

1. **The published artifact is behind the repository**
   ([F1](#f1--the-published-artifact-is-stale-and-misdescribes-the-package-p1--partly-resolved)).
   The in-tree metadata that misdescribed it — a GTK summary and a duplicated
   version — has been fixed and is now pinned by tests, and the local release
   gate (`./bless`) exists; what remains is that the index still serves 1.0.12
   with the old metadata because 2.0.0 has never been published.
2. **Inhibition was decided by parsing human-readable command output and
   ignoring exit status**
   ([F3](#f3--inhibition-is-decided-by-parsing-a-table-and-ignoring-exit-status-p1--resolved)).

Four findings have since been closed outright: the shipped units retried
permanent failures forever
([F2](#f2--the-shipped-units-retried-permanent-failures-indefinitely-p1--resolved)),
inhibition now comes from logind's machine-readable property
([F3](#f3--inhibition-is-decided-by-parsing-a-table-and-ignoring-exit-status-p1--resolved)),
the action layer has tests that pin the exact command per desktop
([F4](#f4--the-action-layer-is-not-exercised-p2--resolved)), and coverage is
measured with a floor CI enforces
([F7](#f7--there-is-no-coverage-measurement-p3--resolved)).

The remaining structural item has also been closed: the idle state machine, the
action layer and the desktop-detection helpers now live in their own modules,
and `PwrTray` is a composition root plus a thin facade
([F6](#f6--one-1115-line-class-holds-every-concern-p2--resolved)). The log is
capped ([F5](#f5--debuglog-is-never-rotated-p2)).

---

## 2. What the program is and how a run works

A PyQt5 tray applet that owns idle policy (lock, then optionally sleep/power off),
shows why the machine is inhibited, and can inhibit locking/sleeping on request.
Policy is per battery tier; behaviour is per desktop, chosen from a YAML table.

### 2.1 Modules

| File | Lines | Responsibility |
| --- | ---: | --- |
| [`pwr_tray/main.py`](../pwr_tray/main.py:1) | 1,310 | Composition root: Qt timer/tray wiring, dirty-flag scheduler, menu, icons, event handlers, `main()`, and thin delegates to the modules below |
| [`pwr_tray/IdleStateMachine.py`](../pwr_tray/IdleStateMachine.py:1) | 97 | The idle branch ladder: lock / blank / suspend / power off / reset / awake, plus the idle-time and state primitives |
| [`pwr_tray/Actions.py`](../pwr_tray/Actions.py:1) | 206 | The user-action layer: every menu entry that starts a process, with the shell semantics of `commands.yaml` |
| [`pwr_tray/Desktop.py`](../pwr_tray/Desktop.py:1) | 166 | DE detection, command validation and resource installation over `commands.yaml` |
| [`pwr_tray/PowerEvents.py`](../pwr_tray/PowerEvents.py:54) | 296 | Event sources: two long-lived `dbus-monitor` children (system bus, session bus) plus a config-file watcher |
| [`pwr_tray/IniTool.py`](../pwr_tray/IniTool.py:49) | 336 | `config.ini` parsing, tier inheritance, defaults, list parsing, change detection |
| [`pwr_tray/Utils.py`](../pwr_tray/Utils.py:1) | 370 | Console capture, logging, process discovery/killing, `flock` single-instance lock |
| [`pwr_tray/SwayIdleMgr.py`](../pwr_tray/SwayIdleMgr.py:19) | 123 | Builds and supervises the `swayidle` command line on Wayland |
| [`pwr_tray/Errors.py`](../pwr_tray/Errors.py:1) | 98 | Three user-facing exceptions with actionable messages |
| [`pwr_tray/service.py`](../pwr_tray/service.py:1) | 534 | `--svc-*` verbs: desktop-to-unit mapping, install-time rendering, log-directory creation, status probe, plan/execute split |
| `pwr_tray/resources/` | — | `commands.yaml`, 9 SVG/PNG icons installed at first run, and the 10 `systemd` units `--svc-install` copies out |

### 2.2 Startup sequence

[`main()`](../pwr_tray/main.py:1248) parses arguments, short-circuits the
non-GUI flags (`--list-des`, `--edit-config`, `--follow-log`), then:

1. constructs [`IniTool`](../pwr_tray/IniTool.py:49), which creates
   `~/.config/pwr-tray/config.ini` and copies the resources if missing;
2. registers the log path and calls
   [`Utils.capture_console()`](../pwr_tray/Utils.py:49) *before* the first
   `prt()`, because `prt()` may redirect fd 2 into `debug.log` and would
   otherwise swallow startup errors;
3. takes the `flock` through
   [`acquire_lock_or_take_over()`](../pwr_tray/main.py:1221) and registers
   `atexit` handlers for the lock and for
   [`PwrTray.goodbye`](../pwr_tray/main.py:1114);
4. waits up to a bounded time for a StatusNotifierWatcher via
   [`wait_for_tray()`](../pwr_tray/main.py:1150) — no watcher is a warning, not
   an error;
5. builds [`PwrTray`](../pwr_tray/main.py:189), where DE detection
   ([`detect_de`](../pwr_tray/main.py:104)) and resource/command checks can fail
   with a typed error, reported to both the log and the console and exiting 2;
6. enters the Qt event loop.

The lock is taken **before** anything destructive and is the only path to killing
another instance, which is opt-in via `--replace` and re-validates the recorded
pid's command line before signalling it
([`PyKill.kill_pid`](../pwr_tray/Utils.py:264)).

### 2.3 Runtime model

* **Timer:** one 100 ms `QTimer`
  ([`main.py:318`](../pwr_tray/main.py:318)) whose interval is recomputed each
  tick; `poll_100ms` forces the fast cadence after something changed.
* **Cheap tick:** [`on_timeout()`](../pwr_tray/main.py:690) only acts on dirty
  flags — `_inhibit_dirty`, `_battery_dirty`, `_config_dirty`. When an event
  source is unavailable for an input, that input is marked dirty every tick, so
  the same code degrades to the old polling behaviour per input rather than
  globally.
* **Slow branch:** every `loop_sample` ticks (15; 1 under `--quick`), it reads
  idle time and runs the state machine: `LockOnly`, `SleepAfterLock`,
  `Presentation`, `NoSleep`, with `power_down` selecting
  [`poweroff()`](../pwr_tray/main.py:1034) over
  [`suspend()`](../pwr_tray/main.py:1012)
  ([main.py:755](../pwr_tray/main.py:755)).
* **Event sources:** [`PowerEvents`](../pwr_tray/PowerEvents.py:54) watches logind
  inhibitor properties and `PrepareForSleep`, UPower AC/device changes, MPRIS
  playback status, and the config file. Monitors are drained through
  `QSocketNotifier`s, lines are classified by object path
  ([`_context_for`](../pwr_tray/PowerEvents.py:250)), and the whole thing is
  stopped idempotently on exit.
* **Inhibition:** [`check_inhibited()`](../pwr_tray/main.py:691) asks logind for
  `BlockInhibited` (via `busctl`) and reads `playerctl status`; a blocked `sleep`
  or `idle` wins over media playback, the `systemd-inhibit` listing supplies the
  who/why rows, and the kind of inhibition decides the icon and whether the
  `swayidle` command keeps its timeouts.

### 2.4 Configuration

`[Settings]` supplies values to `[HiBattery]`/`[LoBattery]`, and an explicit
per-tier key overrides the inherited one
([`_tier_base`](../pwr_tray/IniTool.py:220),
[`_apply_section`](../pwr_tray/IniTool.py:234)). Lists are validated by
[`to_array()`](../pwr_tray/IniTool.py:19); malformed input falls back to the
default instead of raising, and the user-facing rotate state is persisted
separately in `picks.json`
([`save_picks`](../pwr_tray/main.py:432),
[`restore_picks`](../pwr_tray/main.py:461)).

### 2.5 Tests, packaging, CI

* 22 test modules and their shared isolation in
  [`conftest.py`](../conftest.py:97): `HOME` is redirected to a temporary
  directory for every test, and modules that cannot be imported on the runner
  are replaced by permissive stubs. The idle state machine and the action-state
  transitions now have their own process-free unit modules, so the extracted
  collaborators are tested without `object.__new__(PwrTray)`.
* Gate-level tests rather than smoke tests: config inheritance, degenerate
  `config.ini` and `picks.json`, icon loading, DE detection failures, event-line
  classification, dirty-flag coalescing, single-instance behaviour, tray waiting,
  and the shipped unit files are all asserted.
* Coverage is part of the same gate, not a tool nobody runs: branch coverage
  over `pwr_tray` with
  [`fail_under = 75`](../pyproject.toml:154) in `pyproject.toml`, so
  `pytest --cov=pwr_tray` -- the command CI runs on 3.11, 3.12 and 3.13 -- fails
  below the floor.
* Packaging is flit; `pyproject.toml` declares Python >= 3.11, three runtime
  dependencies (`psutil`, `PyQt5`, `ruamel.yaml`), and the resources directory in
  the sdist.
* CI ([`.github/workflows/tests.yml`](../.github/workflows/tests.yml:1)) runs ruff,
  `compileall`, and pytest on 3.11/3.12/3.13, then builds sdist+wheel, asserts the
  wheel's metadata and contents, installs it into a clean virtualenv, and runs
  `--version` and `--list-des` from the installed console script.

---

## 3. Evidence

Run in this tree:

```
$ ruff check .
All checks passed!

$ python3 -m compileall -q pwr_tray conftest.py tests
rc=0

$ python3 -m pytest -q
416 passed in 1.41s

$ python3 -m pytest -q --cov=pwr_tray --cov-report=term-missing
Name                      Stmts   Miss Branch BrPart  Cover   Missing
...
pwr_tray/main.py            953    302    288     38  66.5%   109-116, 233->238, ...
pwr_tray/service.py         456     29    184     17  92.8%   135-140, 189, ...
---------------------------------------------------------------------
TOTAL                      2208    485    752     98  76.3%
Required test coverage of 75.0% reached. Total coverage: 76.32%
416 passed in 2.55s

$ python -m pwr_tray.main --version
pwr-tray 2.0.0

$ python -m pwr_tray.main --list-des
desktops known to pwr-tray (from built-in commands.yaml):
  cinnamon-x11      requires: cinnamon-session-quit [!] systemctl xprintidle xset
  hyprland-wayland  requires: hyprctl [!] swayidle swaylock systemctl
  ...
  [!] = not found on $PATH (install it before using that desktop)
force a desktop with: pwr-tray --de NAME
```

Static totals: 9,211 lines of Python across 10 modules, `conftest.py`, and 22
test modules -- 4,020 of them in `pwr_tray/`, 5,048 in `tests/`.
[`main.py`](../pwr_tray/main.py:1) holds 68 functions, 58 of them methods of
[`PwrTray`](../pwr_tray/main.py:51); the count is deliberately unchanged because
the menu and the historical static call surface are preserved, but the bodies
now delegate to `Actions`, `IdleStateMachine` and `Desktop`.

---

## 4. Findings

Severity: **P1** = will misbehave for a real user; **P2** = maintenance or
diagnostic cost; **P3** = polish.

### F1 — The published artifact is stale and misdescribes the package (P1) — partly resolved

The finding had two halves. The half about **this tree's metadata** is closed;
the half about **what the index serves** is not.

#### In-tree metadata: fixed

* There is no `version =` in `pyproject.toml` any more. It declares
  `dynamic = ["version"]` ([`pyproject.toml:73`](../pyproject.toml:73)), so the
  only copy is [`pwr_tray/__init__.py:3`](../pwr_tray/__init__.py:3), and it
  reads **2.0.0**. A second copy is exactly how the published metadata drifted
  from the code before.
* The summary ([`pyproject.toml:70`](../pyproject.toml:70)) now reads "A PyQt5
  tray applet for power management on i3, sway, niri, Hyprland, and KDE" — the
  real toolkit and both omitted Wayland desktops.
* The dependency list no longer carries the unused `pydbus` or the
  `importlib-metadata; python_version < "3.8"` marker that could never apply
  with `requires-python = ">=3.11"`.
* All of it is pinned by [`tests/test_metadata.py`](../tests/test_metadata.py:1):
  [`test_version_comes_from_the_package_only`](../tests/test_metadata.py:24)
  rejects a `version =` key and requires `dynamic` to name `version`,
  [`test_summary_names_the_real_toolkit`](../tests/test_metadata.py:39) rejects
  `gtk` and requires `pyqt5`, and
  [`test_no_unreachable_importlib_metadata_dependency`](../tests/test_metadata.py:50)
  keeps the dead marker out. The regression the finding describes cannot be
  reintroduced without a red build.

#### The index: unchanged

Queried again while updating this section (2026-10-30, tree at `11fabef`), the
PyPI JSON API still reports:

```
version:         1.0.12
summary:         A GTK tray applet for power management for i3/sway/KDE
requires_python: >=3.8
requires_dist:   ['importlib-metadata; python_version < "3.8"', 'psutil>=5.9',
                  'PyQt5>=5.15', 'ruamel.yaml>=0.17', 'pydbus>=0.6.0']
```

`1.0.13` never existed — the `releases` map jumps from `1.0.12` to nothing — and
`2.0.0` has never been uploaded. So anyone installing from the index still
receives the GTK-era code and the GTK-era metadata, while this tree is at
2.0.0 with the current 400-plus-test suite behind it. The repository is the
source of truth and the index is not.

The packaging job still builds and inspects
([`.github/workflows/tests.yml:95`](../.github/workflows/tests.yml:95)) and never
uploads or verifies against the index — but that is now a decision rather than
an omission: the upload is a local, auditable `./bless publish`
(plan R2), not a job holding a credential.

#### The local release gate: built

[`bless`](../bless:1) is the entry point, and it is a verb because it is one: a
dry run unless given `test` or `publish`.

* **The duplicate check is mandatory in every mode and runs first.** Before the
  gate, before the build, `check_index()` reads the JSON index the mode writes
  to and refuses when the version it would upload is already in the `releases`
  map, printing **every published version** in the refusal. Forgetting a
  version bump costs a network round trip, not a build and a rejected upload.
* The rest of the gate: clean tree, absent tag, `twine>=7` and
  `packaging>=26`, ruff, `compileall`, pytest with coverage, a build into
  `build/bless-<version>/` (wiped first), inspection, `twine check --strict`,
  and a smoke install of the built wheel in a throwaway virtualenv.
* `./bless test` rehearses the upload on TestPyPI. The working version is never
  spent on it: a throwaway `X.Y.Z.99.N` is built from a *copy* of the tree, `N`
  being one past whatever TestPyPI already holds for that base (0 the first
  time), and uploaded under its own token variable
  (`TWINE_TESTPYPI_PASSWORD`), with no tag and no push. It then prints the
  install line — `pip cache purge` first, because pip remembers an index it has
  already read, and `--extra-index-url https://pypi.org/simple/` because
  TestPyPI carries no dependencies.
* `./bless publish` then aligns `main` and `dev`, tags `v<version>`, uploads the
  two named files to PyPI, verifies the index's metadata and wheel digest, and
  pushes. It refuses without `TWINE_PASSWORD` before `twine` starts.
* The script carries no copy of this project's identity: `identity()` reads
  `[project] name` from `pyproject.toml`, derives the package holding
  `__version__` from it (`-`/`.` become `_`), takes the console script from
  `[project.scripts]`, and honours `[tool.bless]` for the module, the inspector
  and the extra smoke argv. A tree that does not declare these is refused with
  both remedies named, and the docstring's "How to reuse" paragraph states what
  another project must provide to use the same file.
* CI inspects with the same [`tests/inspect_dist.py`](../tests/inspect_dist.py:1),
  so the artifact a maintainer blesses is the artifact CI already described.

The decisions are recorded in
[`plans/pypi-release-plan.md`](../plans/pypi-release-plan.md:1) (R1 as amended
on 2026-10-30: a throwaway TestPyPI rehearsal is welcome, the real upload stays
local and owner-run, and CI holds no credential). What was a plan is now a
script that runs, and
[`tests/test_bless.py`](../tests/test_bless.py:1) pins the parts that must not
be wrong: the refusal lists every release, the check precedes every other
stage, the dry run cannot reach tag/upload/push, `publish` reaches them in
order, an upload without a token never starts `twine`, the rehearsal uploads a
dotted version to TestPyPI and tags nothing, the branch table fast-forwards in
both directions and refuses divergence, and the inspector rejects a GTK summary
or a `pydbus` dependency.
[`tests/test_repo_hygiene.py`](../tests/test_repo_hygiene.py:84) keeps
`./bless` executable, requires every releasing document to name it, and refuses
any document that names the old `./release` or uploads the whole `dist/`
directory.

#### What is left

The code is ready and the gate is built; **2.0.0 has never been published**.
`./bless` is the dry run to read, `./bless test` installs the same artifact from
TestPyPI to try it for real, and `./bless publish` is the one irreversible
command. CI could cheaply add a read-only assertion that the index serves the
`__version__` this tree builds, which catches drift without giving a job the
upload credential.

### F2 — The shipped units retried permanent failures indefinitely (P1) — resolved

All ten shipped units formerly used `Restart=always` with `RestartSec=5` and no
`RestartPreventExitStatus` — e.g.
[`pwr-tray-niri.service:33`](../pwr_tray/resources/systemd/pwr-tray-niri.service:33).
That had two consequences.

Two exit codes are permanent, not transient:

* **3** — another instance holds the lock
  ([main.py:1353](../pwr_tray/main.py:1353));
* **2** — the applet could not be constructed at all: unresolved desktop,
  missing resources, or a required command absent from `PATH`
  ([main.py:1377](../pwr_tray/main.py:1377)).

And with `RestartSec=5` the unit restarted at half systemd's default rate-limit
window (`StartLimitIntervalSec=10s`, `StartLimitBurst=5`), so the limiter was
never tripped: two supervisors on one session, or a unit enabled on a desktop
whose commands are missing, produced a silent 5-second loop forever.

Every unit now carries `RestartPreventExitStatus=2 3`, so those cases surface as
a failed unit with the applet's message in the unit log instead of a loop, and
an explicit `StartLimitIntervalSec=60`/`StartLimitBurst=5` pair lets the limiter
stop a genuine crash loop after five attempts in a minute. Both properties are
asserted for every unit in
[`tests/test_service_examples.py`](../tests/test_service_examples.py:117), which
also checks the prevented codes against the exits
[`main()`](../pwr_tray/main.py:1248) actually uses, so the units cannot silently
diverge from the code again.

The units also moved from `docs/examples/` into `pwr_tray/resources/systemd/`,
so they ride along in the wheel and are installed by `pwr-tray --svc-install`
instead of being copied by hand.

### F3 — Inhibition is decided by parsing a table and ignoring exit status (P1) — resolved

[`check_inhibited()`](../pwr_tray/main.py:691) used to run
`systemd-inhibit --no-pager --mode=block` and decide from the
**human-readable** listing: a line containing `block`, `count == 1` skipped
when it belonged to `xfce4-power-man`, `org_kde_powerde`, or Niri's
`handle-power-key`, and anything from row 2 onward counting as an inhibitor.
`result.returncode` was never consulted, and neither was the status of the
`playerctl status` call.

Consequences if the output format, column layout, locale, or the tool's
availability changed: the parse degraded silently to "not inhibited", which
fails open toward locking and suspending — including while media played or
while another application held an inhibitor. The allow-list was also the only
thing preventing a desktop's own power daemon from being read as a user
inhibitor, and it matched by substring against a translated field.

The deeper problem was the question, not the parse: *any* block inhibitor on
*any* operation counted, so the exception list existed to undo that. logind
already answers the question that matters, in a machine field —

```
$ busctl get-property org.freedesktop.login1 /org/freedesktop/login1 \
      org.freedesktop.login1.Manager BlockInhibited
s "sleep:handle-power-key"
```

— and the verdict is now `blocked & GUARDED_OPERATIONS`, i.e. `sleep` or
`idle`: the two operations this applet's own timeouts perform
([`GUARDED_OPERATIONS`](../pwr_tray/main.py:63),
[`parse_block_inhibited`](../pwr_tray/main.py:66),
[`_logind_blocked`](../pwr_tray/main.py:628)). A compositor owning
`handle-power-key` or `handle-lid-switch` is holding an input event, not asking
the session to stay awake, so the per-desktop allow-list is gone rather than
translated.

`busctl` is used instead of a D-Bus binding because it ships with systemd,
which the applet already requires, and because an exit status — unlike a parsed
table — means what it says. The `systemd-inhibit` listing is still read when
something *is* blocked, because the menu shows who and why; that is display
only, and an unreadable listing costs rows, not the verdict
([`_inhibitor_rows`](../pwr_tray/main.py:656)). The common case (nothing
blocked) costs the same two queries as before.

A failed or unreadable query is now "unknown", which counts as not inhibited —
this applet exists to lock the screen, so an unreadable inhibit state must
never switch locking off — and it is logged once per distinct failure instead of
vanishing ([`_warn_inhibit_source`](../pwr_tray/main.py:621)). Media playback
still inhibits on its own, independently of logind.

Both answers are pinned by tests:
[`test_sleep_or_idle_blocked_is_an_inhibitor`](../tests/test_media_inhibit.py:179)
and
[`test_an_input_event_is_not_an_inhibitor`](../tests/test_media_inhibit.py:188)
cover the decision (including the real `handle-power-key` value this project was
developed against),
[`test_a_real_inhibitor_outranks_media_playback`](../tests/test_media_inhibit.py:201)
covers the rows, and
[`test_a_failed_query_warns_once_and_clears_the_inhibit`](../tests/test_media_inhibit.py:222)
covers the failure path.

### F4 — The action layer is not exercised (P2) — resolved

Policy decisions were tested; the commands they trigger were not. In
[`tests/test_power_events.py:253`](../tests/test_power_events.py:253) `poweroff`,
`lock_screen` and `blank_primitive` are recording lambdas; the same is true of
`build_menu`
([`tests/test_power_events.py:180`](../tests/test_power_events.py:180)). Real
command construction in [`suspend`](../pwr_tray/main.py:1207),
[`poweroff`](../pwr_tray/main.py:1229),
[`lock_screen`](../pwr_tray/main.py:1239),
[`blank_primitive`](../pwr_tray/main.py:1245),
[`run_command`](../pwr_tray/main.py:1094) and
[`edit_config`](../pwr_tray/main.py:1162) had no test at all — and that is where
the desktop knowledge lives: `swaylock` arguments, `swaymsg "output * dpms off"`
vs `hyprctl dispatch dpms off` vs `niri msg action power-off-monitors`,
`sleep 1.0; exec xset dpms force off`, `loginctl lock-session`, `systemctl
suspend`, and the rule that KDE, XFCE and Cinnamon lock themselves on suspend.

[`tests/test_actions.py`](../tests/test_actions.py:1) now drives every one of
those methods with a recording `subprocess`, against the commands the applet
really ships: it reads `commands.yaml`, merges it with the same
[`build_variables`](../pwr_tray/main.py:139) that `__init__` uses, and asserts
the exact string handed to the shell for all ten desktops
([`test_lock_screen_runs_this_desktops_locker`](../tests/test_actions.py:227),
[`test_wm_actions_match_the_declared_command`](../tests/test_actions.py:315)).
Sequencing is pinned too: `blank_quick` must lock *before* blanking, and suspend
must not start a locker on a full DE
([`test_blank_quick_locks_before_blanking`](../tests/test_actions.py:247),
[`test_suspend_locks_first_on_a_tiling_desktop`](../tests/test_actions.py:278)).

Writing the tests found a real defect, now removed. `run_command()` chose between
`shell=True` and `command.split()` with `re.match(r'^(\s\w\-)*$', command)` — a
pattern that can only ever match the empty string, so every command had always
gone through `/bin/sh` and the argv branch was dead code. The branch is gone and
a shell is documented as the contract, which it has to be: the values depend on
`~` expansion, quotes and `;`
([`test_run_command_hands_the_line_to_the_shell_untouched`](../tests/test_actions.py:325)).

What is still not covered is whether the *programs* do what the arguments promise.
`swaymsg`/`hyprctl` failures are logged (`NOTE: returncode=...`) but the policy
layer does not act on them, and nothing runs a real locker. That needs a desktop,
not a test.

### F5 — `debug.log` is never rotated (P2) — resolved

At the time of the audit [`IniTool.log_path`](../pwr_tray/IniTool.py:75) was the
only reference: nothing truncated, rotated, or capped it, so a long-lived
session with `debug_mode` on grew the file without bound.

[`Utils.rotate_log()`](../pwr_tray/Utils.py:40) now caps the log at 2 MiB
(`prt_kb = 2048`) and keeps exactly one rotated copy: when the file that `prt()`
is appending to exceeds the cap, it is `os.replace()`d onto `debug.log.old`
(any previous `.old` is discarded) and the reopen creates a fresh `debug.log`.
The rotation only fires when stdout is not a tty, so a `-o/--stdout` run never
rotates a file it is not writing. `-f/--follow-log` still works because
`tail -F` reopens the log by name.

The same log is now reachable from the tray: **≡ Open Applet Log** runs
`less --follow-name +F debug.log [debug.log.old]` inside a terminal window
(the applet has no tty of its own, so a terminal prefix is taken from the
`log_viewer` setting or auto-detected).

What remains outside the applet's control: the shipped units append
`StandardOutput`/`StandardError` to files that systemd does not rotate, so a
deployment that redirects there still needs its own logrotate/logrotate-style
handling.

### F6 — One 1,115-line class holds every concern (P2) — resolved

`PwrTray` used to hold DE detection helpers, config access, icon selection, menu
construction, inhibit evaluation, the idle state machine, `swayidle` lifecycle,
and every user action. The concerns now live where their tests do:

* [`pwr_tray/Desktop.py`](../pwr_tray/Desktop.py:1) — DE detection, command
  validation, variable expansion and resource installation. `PwrTray` keeps
  static aliases, so `--list-des`, the `--svc-*` verbs and the tests keep their
  entry points.
* [`pwr_tray/Actions.py`](../pwr_tray/Actions.py:1) — every action that starts a
  process. `Actions` is handed the applet explicitly instead of reaching for
  `PwrTray.singleton`, and the command-line/shell knowledge now sits next to the
  tests that pin it ([`tests/test_actions.py`](../tests/test_actions.py:1)).
* [`pwr_tray/IdleStateMachine.py`](../pwr_tray/IdleStateMachine.py:1) — the idle
  branch ladder and the idle-time/state primitives. `on_timeout()` is now only
  the scheduler; the once-per-sample decision is a collaborator, and the tick
  tests drive the real class rather than a stand-in.

`PwrTray` is consequently a composition root plus a facade: it wires Qt, the
event sources and the menu, and delegates. The method count is deliberately
unchanged, because removing the delegating entry points would move the menu and
the historical static call surface (`PwrTray.suspend(None)`, ...) as well; what
changed is that no concern's implementation lives in the class any more.

Automated tests never touch the machine's power state. The action layer is
driven by a recorded fake `subprocess`, and [`conftest.py`](../conftest.py:97)
installs an autouse tripwire that makes any un-faked call through `Actions`
fail instead of locking the session or powering the machine off. Real
power-state verification remains a manual step, by design.

### F7 — There is no coverage measurement (P3) — resolved

`[project.optional-dependencies]` listed `pytest` and `ruff`; CI ran both and no
coverage tool, so "285 tests" was a count, not a statement about which lines run.
The action layer in [`tests/test_actions.py`](../tests/test_actions.py:1) was
pinned by hand-written expectations; a coverage report is what says which action
branches nobody has written one for.

Coverage is now measured as part of the gate rather than kept as an optional
habit:

* `pytest-cov>=5` is in the `dev` extra;
* [`[tool.coverage.run]`](../pyproject.toml:139) sets `branch = true` and
  `source = ["pwr_tray"]`, because the misses that matter here are the arms of
  an `if` nobody takes, not only statements nobody executes;
* [`[tool.coverage.report]`](../pyproject.toml:146) carries
  [`fail_under = 75`](../pyproject.toml:154), and pytest-cov reads that floor
  from `pyproject.toml` itself, so CI and a developer running the documented
  command enforce one number instead of two;
* CI runs `python -m pytest --cov=pwr_tray --cov-report=term-missing` on every
  supported Python
  ([`.github/workflows/tests.yml:93`](../.github/workflows/tests.yml:93)).

Measured when the gate was introduced: **76.3% total** -- 2,208 statements and
752 branches, of which 485 statements and 98 partial branches are missed. The
figure is identical on 3.11 and 3.13, and identical with the real
PyQt5/psutil/ruamel.yaml and with `conftest`'s stubs, so it does not depend on
whether the runner has a desktop stack. Per module:

| Module | Cover |
| --- | ---: |
| [`Errors.py`](../pwr_tray/Errors.py:1) | 90.7% |
| [`IniTool.py`](../pwr_tray/IniTool.py:49) | 93.5% |
| [`service.py`](../pwr_tray/service.py:1) | 92.8% |
| [`Utils.py`](../pwr_tray/Utils.py:1) | 73.7% |
| [`PowerEvents.py`](../pwr_tray/PowerEvents.py:54) | 71.4% |
| [`main.py`](../pwr_tray/main.py:1) | 66.5% |
| [`SwayIdleMgr.py`](../pwr_tray/SwayIdleMgr.py:19) | 63.6% |

That distribution is the answer the finding asked for, and the line numbers
distinguish two kinds of miss. The first needs a session and cannot be tested
here: `PwrTray.__init__` and the menu it builds (lines 248-404), the
`QTimer`/menu-refresh wiring (812-961), and
[`SwayIdleMgr`](../pwr_tray/SwayIdleMgr.py:19)'s supervisor
([102-123](../pwr_tray/SwayIdleMgr.py:102)). The second is simply a method nobody
has written a test for yet -- [`update_battery_status`](../pwr_tray/main.py:770)
and [`set_state`](../pwr_tray/main.py:805), for instance -- which is precisely
the list that had to be reconstructed by hand before. The action methods
themselves are pinned by [`tests/test_actions.py`](../tests/test_actions.py:1);
the report keeps the remainder in line numbers instead of in memory.

The floor is deliberately just under the measured value: it is a ratchet against
new code arriving untested, not a target. The gate cannot quietly rot away,
either: `tests/test_repo_hygiene.py` asserts the dependency, the branch setting,
the floor, the CI command, the documented command, and the ignored `.coverage`
file.

### F8 — The units are syntax-checked, not field-tested (P3) — partly mitigated

[`tests/test_service_examples.py`](../tests/test_service_examples.py:1) validates
structure, required directives, and that each unit's `--de` key exists in
`commands.yaml`. The README states plainly that the units have not been run on
every desktop, and that is the honest position: unit correctness against a live
session manager, tray host, and idle daemon cannot be asserted in CI.

What changed since: the units now ship in the wheel and are installed from the
artifact by `pwr-tray --svc-install`, so "the file a user runs" and "the file the
tests validate" are the same bytes -- except for the two machine-specific values
`render()` substitutes, which
[`tests/test_service_examples.py`](../tests/test_service_examples.py:175) checks
leave every other directive intact. `pwr-tray --svc-status` is also a runtime
probe of the installed unit, so a user can tell a syntax-valid unit from one the
manager actually started. Field-testing each desktop is still open.

The first field test proved the point. Installing the niri unit on a real
session and starting it failed with `Process: ExecStartPre=/bin/sleep 1
(code=exited, status=209/STDOUT)` and, in the journal, `Failed to set up
standard output: No such file or directory`: the units capture stdout/stderr
into `%h/.local/log`, which systemd does not create, and only the *manual*
recipe had ever created it. The unit then sat in `failed` because the start
rate limiter, correctly, gave up. Both are now policy rather than folklore:
`output_dirs()`/`missing_dirs()` read the paths back out of the unit so
`--svc-install` creates them (and prints `create /home/.../.local/log` in the
plan), and a unit already in the `failed` state is cleared with
`systemctl --user reset-failed` before it is started. Neither defect was
reachable from the shipped text alone; both are covered by
[`tests/test_service_commands.py`](../tests/test_service_commands.py:1) now.

---

## 5. What is done well

* **Single-instance guarantee.** `flock` first
  ([`SingleInstance.acquire`](../pwr_tray/Utils.py:326)), an opt-in `--replace`
  that re-verifies argv before signalling, and `atexit` release. The failure mode
  is a clean exit 3 with the holder's pid, not a duplicate applet.
* **Event-driven core with a per-input fallback.** `PowerEvents` removes the
  per-tick `dbus-monitor` spawns, and `on_timeout()` re-enables polling for
  exactly the input whose source died
  ([main.py:700](../pwr_tray/main.py:700)) instead of abandoning the design
  wholesale.
* **Errors that name the fix.** [`Errors.py`](../pwr_tray/Errors.py:1) turns "no
  desktop detected" into a message listing the known desktops and both escape
  hatches, and `missing_commands()`
  ([main.py:98](../pwr_tray/main.py:98)) names the absent binary plus how to list
  requirements (`--list-des`, verified above).
* **Config robustness.** Tier inheritance with explicit overrides, malformed
  lists falling back rather than crashing, degenerate `config.ini`/`picks.json`
  surviving, and lock/sleep picks restored on independent axes.
* **Diagnostics ordering.** Console capture before log redirection means startup
  failures reach the terminal that launched the applet.
* **Packaging discipline.** Wheel contents, metadata, and the installed console
  script are asserted in CI, not assumed; the sdist carries the resources.
* **Test isolation and speed.** 416 tests in 1.4 s (2.6 s with coverage) with
  `HOME` redirected, so the suite cannot read or write a developer's real
  configuration.

---

## 6. Value relative to the alternatives

| Option | Covers | Gap it leaves |
| --- | --- | --- |
| `swayidle` / `hypridle` config | Lock + sleep timers on one compositor | No on-screen state, no inhibitor visibility, no per-battery-tier policy, no manual "keep awake" toggle |
| DE power manager (Powerdevil, XFCE, Cinnamon, MATE, LXQt) | Native, integrated, vendor-maintained | Confined to its own DE; conflicts with `swayidle`-style tools; no uniform behaviour when you switch WMs |
| `systemd-inhibit` wrappers / caffeine-style tools | One-shot "don't suspend now" | No scheduling, no sequencing of lock→sleep, no feedback about *who* is inhibiting |
| Hand-rolled shell scripts | Exactly your policy | You own the state machine, the inhibit parsing, the tray, and the edge cases across DEs |

`pwr-tray` occupies the niche the table cannot fill: **one policy engine and one
tray UI across ten desktops**, with lock→sleep sequencing, per-battery-tier
timers, visible inhibition, and a manual override — the same behaviour on i3,
sway, niri, Hyprland, KDE, XFCE, Cinnamon, MATE, and LXQt.

Its cost is one always-on Qt process, up to two `dbus-monitor` children, a Python
3.11 + PyQt5 runtime, and desktop knowledge encoded in `commands.yaml` that has
to be kept current.

Adoption is effectively nil — 2 stars, 0 forks, 0 open issues on GitHub, and the
index still serving 1.0.12. That does not diminish the engineering, but it means
the value is **fit for one person's multi-desktop setup**, not an ecosystem
role. As a general recommendation to a stranger, a single `hypridle` or
Powerdevil configuration is the lower-risk choice; as a portable, inspectable
policy layer with a tray, this is the more capable one.

**Bottom line:** worth keeping and worth finishing — the remaining work is one
upload, not metadata, tooling or redesign: the tree describes itself correctly,
the release gate is built and tested (F1's in-tree half is closed), so what is
left is `./bless publish` for 2.0.0. The structural item (F6) is done.

---

## 7. Suggested order of work

1. **F1 — metadata and tooling done, publication open.** The summary, the
   single-source version and the dependency list are fixed and pinned by
   [`tests/test_metadata.py`](../tests/test_metadata.py:1); the local gate
   ([`bless`](../bless:1), [`tests/inspect_dist.py`](../tests/inspect_dist.py:1),
   [`tests/test_bless.py`](../tests/test_bless.py:1)) is built and shares its
   inspector with CI. `1.0.13` never existed and `2.0.0` is unpublished. Run
   `./bless` (dry run; its duplicate check lists every published release),
   then `./bless publish`. Uploads stay local (plan R2); CI at most asserts
   that the index serves the version this tree builds.
2. **F2 — done.** `RestartPreventExitStatus=2 3` and
   `StartLimitIntervalSec=60`/`StartLimitBurst=5` are on all ten units, and
   [`tests/test_service_examples.py`](../tests/test_service_examples.py:1)
   asserts both. The units also moved into `pwr_tray/resources/systemd/` and are
   installed by `pwr-tray --svc-install`, so the policy reaches users instead of
   sitting in `docs/`.
3. **F3 — done.** The verdict now comes from logind's `BlockInhibited`
   ([`_logind_blocked`](../pwr_tray/main.py:628)) and counts only `sleep` and
   `idle`, so the per-desktop allow-list for `xfce4-power-man`,
   `org_kde_powerdevil` and niri is gone and the `systemd-inhibit` listing is
   display only. A failed or unreadable query is logged once and can never be
   mistaken for "inhibited"; both the decision and the failure path are tested
   in [`tests/test_media_inhibit.py`](../tests/test_media_inhibit.py:131).
4. **F4 — done.** [`tests/test_actions.py`](../tests/test_actions.py:1) asserts
   the exact command for every desktop, read from the shipped `commands.yaml`,
   and pins the lock-then-blank order and the suspend-locker rule. It also
   exposed a dead "no shell needed" branch in `run_command()`, now removed.
5. **F7 — done.** `pytest-cov` is in the dev extra, branch coverage is measured
   over `pwr_tray`, and `fail_under = 75` makes the CI command fail below it.
   The next step is the ordinary one: raise the floor as `main.py`'s untested
   arms acquire tests.
6. **F6 — done.** The action layer, the idle state machine and the DE-detection
   helpers moved to `Actions.py`, `IdleStateMachine.py` and `Desktop.py`.
   `PwrTray` is a composition root and facade; the existing public entry points
   are preserved as thin delegates. The tests that used to build a bare
   `object.__new__(PwrTray)` now drive the real `IdleStateMachine`, and the
   shared test setup refuses any un-faked process launch from the action layer.

---

## Appendix — how this was checked

* Read all 7 application modules, `conftest.py`, all 15 test modules,
  `pyproject.toml`, `.github/workflows/tests.yml`, all 10 units (then under
  `docs/examples/`, since moved to `pwr_tray/resources/systemd/`), and the README.
* Ran `ruff check .`, `compileall`, and `pytest -q` in the tree (results in §3).
* Ran the installed CLI (`--version`, `--list-des`) through the pipx virtualenv
  with `PYTHONPATH` pointed at this tree; the applet itself was not launched, to
  avoid terminating a live instance.
* Queried the PyPI JSON API for the published version and summary (F1) and the
  GitHub API for adoption figures (§6).
* Findings F2–F7 were confirmed by reading the cited lines, not by inference from
  the README.
* F1's status was re-checked at `11fabef` on 2026-10-30: the PyPI JSON API for
  the served version, summary, `Requires-Python` and `Requires-Dist`;
  `pyproject.toml`, `pwr_tray/__init__.py` and `tests/test_metadata.py` for the
  in-tree fix; and `./bless` plus `tests/test_bless.py`, which pin the release
  gate and its duplicate check.
