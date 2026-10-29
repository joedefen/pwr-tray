# pwr-tray audit

- **Audited version:** 1.0.12 (`pyproject.toml`), commit `b1b9735`; open findings re-checked on `dev` @ `e07f6f7`
- **Audit date:** 2026-10-29
- **Scope:** all of `pwr_tray/` (1,746 lines), `pyproject.toml`, `runner`, `README.md`, `docs/examples/*.service`, built wheel
- **Method:** full read of every module; static analysis (`ruff`, `compileall`); targeted grep for dead symbols; isolated-`HOME` runtime probe of the config layer; inspection of the built wheel; ADR-style comparison against the existing tool landscape

---

## Verdict

**The idea has real value. This implementation currently only delivers part of it.**

pwr-tray is the only tool I can find that treats power state as a *tri-state tray toggle* (Presentation / LockOnly / SleepAfterLock) with *per-power-source timeout profiles* and *inhibitor visibility*, on one codebase spanning i3, sway, Hyprland, niri, KDE, XFCE, Cinnamon, MATE, LXQt. That combination does not exist elsewhere — that is the value.

What undercuts it:

- two advertised features are dead code (P0-1, P0-2);
- it raises tracebacks instead of reporting errors on unknown DEs and missing binaries;
- it ships a module that cannot be imported;
- it has zero tests.

Adoption reflects this: 2 stars, 1 subscriber, 0 issues, 21 PyPI releases — a well-maintained single-user project, not yet a tool others can safely adopt.

---

## P0 — Advertised features that do not work

### 1. `power_down = True` never powers down

[`_get_down_state()`](../pwr_tray/main.py:325) returns `'PowerDown'`:

```python
def _get_down_state(self):
    return 'PowerDown' if self.get_params().power_down else 'Suspend'
```

but the consumer compares against `'PowerOff'` ([line 536](../pwr_tray/main.py:536)):

```python
if self._get_down_state() == 'PowerOff':
    self.poweroff(None)
else:
    self.suspend(None)
```

The branch is unreachable, so the configured `power_down = True` silently degrades to suspend. Documented at README [line 136](../README.md:136).

### 2. Sleep timeouts are not restored across restarts

[`restore_picks()`](../pwr_tray/main.py:311) calls the *lock* helper for both axes:

```python
for attr in 'lock_mins sleep_mins'.split():
    for selector in 'LoBattery HiBattery Settings'.split():
        self.get_lock_rotated_list(selector, first=picks[attr][selector])
```

`sleep_mins` must use `get_sleep_rotated_list`. Two secondary defects in the same method:

- `picks[attr][selector]` (not `.get`) means a hand-edited or older `picks.json` aborts the entire restore;
- the `except` path ends with `return True`, reporting success after failure, and the return value is never consumed anyway.

---

## P1 — Correctness and UX bugs (verified)

### 3. `HiBattery` / `LoBattery` do not inherit the user's `[Settings]` values

[`update_config()`](../pwr_tray/IniTool.py:127) initialises `running = goldens` and never reassigns it after parsing `[Settings]`, so tiers inherit the *hard-coded defaults*, not the user's overrides.

Verified with an isolated `HOME` and this `config.ini`:

```ini
[Settings]
lock_min_list = [3, 7]
sleep_min_list = [9, 11]
gui_editor = my-edit

[HiBattery]
lock_min_list = [4, 5]
```

Observed output:

```
Settings    lock=[3, 7] sleep=[9, 11] gui_editor='my-edit'
HiBattery   lock=[4, 5] sleep=[1, 10] gui_editor='geany'   <-- should inherit [9, 11] / 'my-edit'
LoBattery   lock=[1, 1] sleep=[1, 1]  gui_editor='geany'   <-- same problem
```

This directly contradicts README [lines 129–130](../README.md:129) ("Missing/invalid settings are inherited from 'Settings'").

**Fix:** `running = params` at the end of each selector iteration.

### 4. A malformed list value yields `None` instead of the default

[`update_config()`](../pwr_tray/IniTool.py:147) tests the raw string rather than the parse result:

```python
list_value = to_array(value)
if not value:                      # BUG: should be `if list_value is None`
    params[key] = self.the_default(selector, key)
    prt(f'skip {selector}.{key}: {value!r} [bad list spec]')
else:
    params[key] = list_value       # may be None
```

Verified: `lock_min_list = not-json` results in `params.lock_min_list is None`.

`get_current_vals()` masks this on most paths, but any direct read of `params.*_list` observes `None`, and the emitted log message reports "bad list spec" for the wrong case.

### 5. Non-importable module shipped in the wheel

[`YamlDump.py`](../pwr_tray/YamlDump.py:18) imports a package that does not exist in this project:

```python
from LibGen.CustLogger import CustLogger as lg
```

```
$ python -c "import pwr_tray.YamlDump"
ModuleNotFoundError: No module named 'LibGen'
```

Confirmed present inside `dist/pwr_tray-1.0.12-py3-none-any.whl`. Nothing imports it. [`PrtUtil.py`](../pwr_tray/PrtUtil.py:5) additionally self-documents as "NOTE: not being used (future considerations)".

**Fix:** delete both modules from the package, then rebuild.

### 6. `assert` used as user-facing error handling

- [`detect_de()`](../pwr_tray/main.py:121): `assert False, f'no DE matched: ...'` — a full traceback for GNOME, Budgie, Deepin, Weston, or any unrecognised session.
- [`main.py:204`](../pwr_tray/main.py:204): `assert not dont_haves, f'commands NOT on $PATH: {dont_haves}'` — same, for a missing optional binary.

README [line 55](../README.md:55) claims `--stdout` "Shows system-level commands (DE dependent) that must be installed if missing". No such reporting code exists.

Worse, under `python -O` both guards vanish and the application proceeds with empty command strings, producing silent no-ops instead of an error.

**Fix:** raise a dedicated exception caught in `main()` and print an actionable message plus the known-DE list; add `--list-des`.

### 7. Restarted instances escape the single-instance killer

[`PyKill._kill_script()`](../pwr_tray/Utils.py:169) only matches a process when `basename(argv[1]) in ('python', 'python3')` **and** `basename(argv[2])` equals the target. [`restart_self()`](../pwr_tray/main.py:760) relaunches as `python -m pwr_tray.main`, where `ps` renders `argv[2]` as `-m` — never matched.

The same applies to the development launcher `./runner`, whose process is `python runner` (and which additionally carries a copy-paste `run the dwipe.main module` docstring at [`runner:18`](../runner:18)).

There is no PID/lock file as a backstop, so a restarted instance can coexist with a subsequently launched one, producing duplicate tray icons and duplicate `swayidle` management.

### 8. Orphaned `dbus-monitor` per restart

[`_start_resume_monitor()`](../pwr_tray/main.py:902) spawns a `dbus-monitor` child that is never terminated. [`restart_self()`](../pwr_tray/main.py:760) exits via `os._exit(0)`, which skips `atexit` and leaves the child running. Each restart leaks one `dbus-monitor`.

### 9. Icon loading depends on a global `os.chdir`

[`main.py:155`](../pwr_tray/main.py:155) tests `os.path.isfile(resource)` with a **relative** path that only resolves because [`IniTool.__init__`](../pwr_tray/IniTool.py:60) calls `os.chdir(self.folder)`:

```python
for resource in self.svgs + ['lockpaper.png']:
    if not os.path.isfile(resource):
        Utils.copy_to_folder(resource, ini_tool.folder)
    if not os.path.isfile(resource):
        prt(f'WARN: cannot find {repr(resource)}')
        continue
    self.icons.append(QIcon(os.path.join(self.ini_tool.folder, resource)))
```

If a copy fails, `self.icons` holds fewer than eight entries and [`show_icon()`](../pwr_tray/main.py:389) raises `IndexError` on `self.icons[num]`. The global `chdir` is also the root cause of the `cwd=` workaround in `restart_self`.

**Fix:** use `os.path.join(ini_tool.folder, resource)` consistently, abort with a clear message if any icon is missing, and drop `os.chdir`.

---

## P2 — Design, cost, and hygiene

### 10. Polling architecture — resolved

Every 2-second tick used to perform:

- `systemd-inhibit --no-pager --mode=block` (process spawn + D-Bus roundtrip);
- `playerctl status` (second process spawn, only while enabled);
- `psutil.sensors_battery()`;
- `os.path.getmtime()` on `config.ini`;
- a non-blocking read of the `dbus-monitor` pipe.

That was roughly 43,000 process spawns per day to observe events logind, UPower
and the MPRIS players already publish as D-Bus signals.

[`PowerEvents`](../pwr_tray/PowerEvents.py:1) now subscribes to those signals
instead of polling for them:

- one long-lived `dbus-monitor` per bus (system: logind `BlockInhibited` /
  `DelayInhibited` and UPower `OnBattery` / battery percentage; session: MPRIS
  `PlaybackStatus`), read through `QSocketNotifier` so the pipes are not polled
  either;
- `QFileSystemWatcher` on `config.ini` *and* its directory, so an editor that
  saves by rename is not missed;
- [`on_timeout()`](../pwr_tray/main.py:677) still runs the cheap state machine,
  but calls `systemd-inhibit`, `playerctl`, `psutil` and `reconfig()` only when
  an event marked that input dirty;
- a five-minute `_resync()` safety net covers a missed signal (a restarted
  service, a player that vanished without one): ~288 queries per day instead of
  43,000.

If `dbus-monitor` or `QtDBus`/`QSocketNotifier` is unavailable — or a monitor
exits — the affected inputs fall back to the old once-per-tick polling, so the
change cannot make behaviour worse.  [`tests/test_power_events.py`](test_power_events.py:1)
pins the signal filtering, event coalescing and the dirty-flag gating.

### 11. Module-level D-Bus dependency

`SESSION_BUS = SessionBus()` at [main.py:40](../pwr_tray/main.py:40) means even `--help` requires a session bus. `pydbus` is effectively unmaintained upstream and is used for a single `NameHasOwner("org.kde.StatusNotifierWatcher")` call in [`wait_for_tray()`](../pwr_tray/main.py:940).

`wait_for_tray(timeout=300)` also blocks silently for up to **five minutes**, and its return value is discarded at [line 1006](../pwr_tray/main.py:1006). On an X11 bar without a StatusNotifierWatcher it burns the full five minutes, then proceeds regardless.

### 12. Metadata drift

- Published summary says "A **GTK** tray applet" ([pyproject.toml:8](../pyproject.toml:8)); the toolkit is PyQt5. This string is live on PyPI for 1.0.12.
- `importlib-metadata; python_version<"3.8"` ([line 22](../pyproject.toml:22)) can never apply given `requires-python = ">=3.8"`.
- `keywords` include `niri` ([line 15](../pyproject.toml:15)) but the README never mentions niri, while [`commands.yaml`](../pwr_tray/resources/commands.yaml:61) ships niri support and the code carries a niri-specific inhibitor workaround ([main.py:399](../pwr_tray/main.py:399)).
- [`sys.path.append('/usr/lib/python3/dist-packages')`](../pwr_tray/main.py:15) mixes system packages into a pipx venv. It is a band-aid predating the `PyQt5` dependency declaration and now mostly obsolete, but still capable of shadowing.
- `pwr_tray/__init__.py` is empty: no `__version__`, and there is no `--version` flag.

### 13. Dead weight

- `svg_info.subdir='resources/SetD'` ([main.py:48](../pwr_tray/main.py:48)) names a directory that does not exist; the field is never read.
- `resources/SetA`, `SetB`, `SetC` ship six SVGs that nothing loads — roughly two-thirds of the SVG payload in the wheel.
- `get_effective_mode()` and `effective_mode()` are byte-identical duplicates ([269](../pwr_tray/main.py:269) / [349](../pwr_tray/main.py:349)).
- Inert state and methods: `lock_began_secs`, `inh_lock_began_secs` (only ever cleared at [line 556](../pwr_tray/main.py:556)), `old_suspend`, `dimmer`, `undim`, and the commented-out dimming configuration in `IniTool`.
- `main.py` carries 11 file-level pylint blanket disables ([line 6](../pwr_tray/main.py:6)).

### 14. Repository hygiene

- No `.github/` — no CI, no lint gate, no packaging smoke test.
- No `tests/` directory (this file is the first artefact in it).
- No linter configuration in-repo; `ruff` under the user's global config reports 52 issues, including 12 blind `except Exception` and 2 `try/except/pass`.
- `.venv/` is not listed in [`.gitignore`](../.gitignore:1) (currently untracked, so this is latent).
- `docs/examples/*.service` are, by their own admission ([README line 208](../README.md:208)), untested.

### 15. Config-path robustness

If the user deletes `config.ini`, `os.path.getmtime()` in [`update_config()`](../pwr_tray/IniTool.py:119) raises, and `update_config()` is called from `on_timeout()` inside a Qt slot — an exception per tick rather than a single recoverable error.

---

## What is genuinely good

These are structural strengths, not consolation prizes:

- **The three-layer `commands.yaml` merge** ([`detect_de()`](../pwr_tray/main.py:93)) with `{{variable}}` expansion and `must_haves` aggregation is a clean, extensible design. Comparable projects hardcode an `if de == 'sway'` ladder; this does not, and adding a DE is a data change.
- **The swayidle cumulative-timeout math** ([`build_cmd()`](../pwr_tray/SwayIdleMgr.py:57)) is correct: the suspend timeout is `lock_s + sleep_s`, all `timeout` clauses precede `before-sleep`/`after-resume`, and `before-sleep` re-locks on lid close. "Kill and replace `swayidle` to express intent" is the right abstraction for a compositor that offers no idle polling.
- **The README is better than most commercial products':** per-DE requirements table, distro caveats (Plasma 5 vs 6, swayidle 1.7 vs 1.8), and the systemd-service caveat section ([lines 221–227](../README.md:221)) preempt exactly the traps users hit.
- **Git history shows reliability work, not feature sprawl:** fd-leak fixes (`ca23ffe`, `c8b0215`), `ConditionEnvironment` hardening (`b1b9735`), tray-readiness waiting (`0372122`).

---

## Value relative to alternatives

| Need | Existing answer | pwr-tray's delta |
|---|---|---|
| Lock / sleep / dpms on sway or Hyprland | `swayidle` + `swaylock`, `hypridle` + `hyprlock`, ~20 lines of config | Same capability, plus a GUI |
| "Do not sleep now" toggle | `caffeine` / `caffeine-ng`, `waybar` idle-inhibitor module, `systemd-inhibit` | Same, but the mode is tri-state and persisted |
| Battery-aware timeouts | `power-profiles-daemon`, `tlp` (power policy, not idle timing) | **Unique**: per-tier lock/sleep profiles |
| Inhibitor awareness | DE applets (`xfce4-power-manager`), `systemd-inhibit --list` | **Unique in tray form for tiling WMs** |
| One UI across nine DEs | nothing comparable | **Unique** |

**Conclusion:** yes, this has value beyond personal preference — but the value is in the **UX model**, not the mechanism. Every capability is reproducible with `swayidle` + `systemd-inhibit` + a Waybar scripted module; nobody has packaged the *model* (three modes, battery tiers, inhibitor icon, timeout rotation) as a DE-agnostic tray. That is a small but genuine niche: laptop users on sway/Hyprland who want a Presentation-mode toggle and battery-adaptive timeouts without editing configs.

The honest counterweight: that target user is exactly the user most comfortable with config files, and `hypridle` listener blocks express Presentation mode in five lines. The addressable market is "people who want the tray, not the config" — real, but thin, which the 2 stars reflect. It will not become a distro-default tool without the P0 fixes, CI, and a Qt6 / `ext-idle-notify` path.

---

## Highest-leverage fix order

1. Fix the `'PowerDown'` / `'PowerOff'` mismatch — [main.py:326](../pwr_tray/main.py:326)
2. Use `get_sleep_rotated_list` in `restore_picks`, with `.get()` lookups — [main.py:311](../pwr_tray/main.py:311)
3. `running = params` inheritance and `if list_value is None` — [IniTool.py:127](../pwr_tray/IniTool.py:127), [IniTool.py:147](../pwr_tray/IniTool.py:147)
4. Delete `YamlDump.py` and `PrtUtil.py`, then rebuild the wheel
5. Replace both asserts with a readable error plus a `--list-des` diagnostic; implement what README line 55 promises
6. Add `--version`; drop `pydbus` in favour of `QtDBus`/`busctl`; terminate `dbus-monitor` on exit; add a PID lock file
7. Add a test suite covering `IniTool` merge/parse and `SwayIdleManager.build_cmd` — both are I/O-light and untested, despite holding all the logic that has already produced the bugs above

---

## Appendix — evidence log

```
$ .venv/bin/python -m compileall -q pwr_tray      -> COMPILE_OK
$ .venv/bin/python -c "import pwr_tray.YamlDump"  -> ModuleNotFoundError: No module named 'LibGen'
$ HOME=/tmp/pt-audit python /tmp/pt-audit/probe.py
    Settings    lock=[3, 7] sleep=[9, 11] gui_editor='my-edit'
    HiBattery   lock=[4, 5] sleep=[1, 10] gui_editor='geany'
    LoBattery   lock=[1, 1] sleep=[1, 1]  gui_editor='geany'
    Settings.lock_min_list = None        # after `lock_min_list = not-json`
$ ruff check pwr_tray --statistics                  -> 52 errors (12 BLE001, 8 I001, 6 UP009, 5 N999, 3 SIM114, ...)
$ python3 -c "zipfile ... pwr_tray-1.0.12-py3-none-any.whl"  -> ships YamlDump.py, PrtUtil.py, resources/SetA|B|C
$ curl api.github.com/repos/joedefen/pwr-tray       -> 2 stars, 0 forks, 0 open issues, 1 subscriber
$ curl pypi.org/pypi/pwr-tray/json                  -> 1.0.12, 21 releases
```
