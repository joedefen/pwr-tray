# pwr-tray — audit

**Package:** `pwr_tray` **2.0.0**
**Tree audited:** working tree at `dd0011f`, 2026-10-30
**Scope:** the whole repository — application modules, tests, packaging, CI, shipped docs/systemd units.
**Question asked:** is this sound, and does it have value relative to the alternatives?

This document describes the code as it stands. Nothing here is a changelog.

---

## 1. Verdict

Sound, deliberately engineered, and small enough to keep sound. The suite is real
(285 tests, 0.91 s) and the lint gate is clean; the architecture separates event
detection from policy and both from presentation. Nothing found is a
use-it-and-lose-data bug.

What was genuinely unfinished fell into two groups:

1. **The published artifact is behind the repository and describes the wrong
   toolkit** ([F1](#f1--the-published-artifact-is-stale-and-misdescribes-the-package-p1)).
2. **Inhibition was decided by parsing human-readable command output and
   ignoring exit status**
   ([F3](#f3--inhibition-is-decided-by-parsing-a-table-and-ignoring-exit-status-p1--resolved)).

Three findings have since been closed: the shipped units retried permanent
failures forever
([F2](#f2--the-shipped-units-retried-permanent-failures-indefinitely-p1--resolved)),
inhibition now comes from logind's machine-readable property
([F3](#f3--inhibition-is-decided-by-parsing-a-table-and-ignoring-exit-status-p1--resolved)),
and the action layer has tests that pin the exact command per desktop
([F4](#f4--the-action-layer-is-not-exercised-p2--resolved)).

The rest is structural: one very large class
([F6](#f6--one-1115-line-class-holds-every-concern-p2)), a log that was never
rotated ([F5](#f5--debuglog-is-never-rotated-p2), since capped), and no coverage
measurement ([F7](#f7--there-is-no-coverage-measurement-p3)).

---

## 2. What the program is and how a run works

A PyQt5 tray applet that owns idle policy (lock, then optionally sleep/power off),
shows why the machine is inhibited, and can inhibit locking/sleeping on request.
Policy is per battery tier; behaviour is per desktop, chosen from a YAML table.

### 2.1 Modules

| File | Lines | Responsibility |
| --- | ---: | --- |
| [`pwr_tray/main.py`](../pwr_tray/main.py:1) | 1,462 | `PwrTray` class (state machine, menu, icons, actions), DE detection, tray waiting, lock takeover, `main()` |
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

* 16 test modules, 285 tests, shared isolation in
  [`conftest.py`](../conftest.py:97): `HOME` is redirected to a temporary
  directory for every test, and modules that cannot be imported on the runner
  are replaced by permissive stubs.
* Gate-level tests rather than smoke tests: config inheritance, degenerate
  `config.ini` and `picks.json`, icon loading, DE detection failures, event-line
  classification, dirty-flag coalescing, single-instance behaviour, tray waiting,
  and the shipped unit files are all asserted.
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
285 passed in 0.91s

$ python -m pwr_tray.main --version
pwr-tray 1.0.13

$ python -m pwr_tray.main --list-des
desktops known to pwr-tray (from built-in commands.yaml):
  cinnamon-x11      requires: cinnamon-session-quit [!] systemctl xprintidle xset
  hyprland-wayland  requires: hyprctl [!] swayidle swaylock systemctl
  ...
  [!] = not found on $PATH (install it before using that desktop)
force a desktop with: pwr-tray --de NAME
```

Static totals: 5,330 lines of Python across 7 modules, `conftest.py`, and 15 test
modules. [`main.py`](../pwr_tray/main.py:1) holds 66 functions, 60 of them methods
of [`PwrTray`](../pwr_tray/main.py:51).

---

## 4. Findings

Severity: **P1** = will misbehave for a real user; **P2** = maintenance or
diagnostic cost; **P3** = polish.

### F1 — The published artifact is stale and misdescribes the package (P1)

`pyproject.toml` says `version = "1.0.13"` and
"*A PyQt5 tray applet for power management on i3, sway, niri, Hyprland, and
KDE*". The latest artifact on PyPI is **1.0.12**, whose summary still reads
"*A GTK tray applet for power management for i3/sway/KDE*" — wrong toolkit, no
niri, no Hyprland. Anyone installing from the index therefore receives a package
whose own metadata misdescribes it, and whose code predates the current test
suite. The repository is the source of truth and the index is not.

There is no release automation in CI: the packaging job builds and inspects
distributions ([`.github/workflows/tests.yml:90`](../.github/workflows/tests.yml:90))
but never uploads or verifies against the index.

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

### F6 — One 1,115-line class holds every concern (P2)

[`PwrTray`](../pwr_tray/main.py:51) is 60 methods covering DE detection helpers,
config access, icon selection, menu construction, inhibit evaluation, the idle
state machine, `swayidle` lifecycle, and every user action. The tests cope by
building instances without running `__init__`
([`tests/test_power_events.py:151`](../tests/test_power_events.py:151) —
`object.__new__(PwrTray)` plus hand-assigned attributes), which means the
constructor's own wiring is only covered indirectly.

The seams that already exist (`PowerEvents`, `SwayIdleMgr`, `IniTool`,
`Errors`) show the intended decomposition; the remaining coupling is inside the
class, not between modules.

### F7 — There is no coverage measurement (P3)

`[project.optional-dependencies]` lists `pytest` and `ruff`; CI runs both and no
coverage tool, so "285 tests" is a count, not a statement about which lines run.
The action layer in [`tests/test_actions.py`](../tests/test_actions.py:1) is now
pinned by hand-written expectations; a coverage report is what would say which
action branches nobody has written one for.

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
* **Test isolation and speed.** 285 tests in 0.91 s with `HOME` redirected, so
  the suite cannot read or write a developer's real configuration.

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

**Bottom line:** worth keeping and worth finishing — the remaining work is
distribution hygiene (F1) and test/ops polish (F6–F7), not redesign.

---

## 7. Suggested order of work

1. **F1** — tag and publish 1.0.13, then add a CI release job that uploads the
   artifacts it already validates, so the index cannot drift from the tree again.
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
5. **F7** — add `pytest-cov` and publish the number in CI.
6. **F6** — if the class is touched again, extract the idle state machine and the
   action layer; both are already well separated conceptually.

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
