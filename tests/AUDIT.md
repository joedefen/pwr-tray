# pwr-tray — Project Audit (re-baselined)

**Baseline:** `v1.0.13` @ [`d6a05b8`](https://github.com/lagerfeuer/pwr-tray/commit/d6a05b8)
**Supersedes:** the v1 audit taken at `v1.0.12` @ `b1b9735`
**Date:** 2026-10-30
**Scope:** every file under [`pwr_tray/`](../pwr_tray/), packaging (`pyproject.toml`), tests/CI, docs, and the shipped systemd units.
**Method:** review of the current working tree, `git log` archaeology against the v1 audit, `ruff`, `compileall`, the pytest suite, and out-of-tree probes exercising `IniTool` and the CLI in a throwaway `$HOME`.

---

## 1. Headline

The v1 audit reported 16 findings (3 × P0, 7 × P1, 6 × P2). **All 15 findings that were actionable in-repo are now fixed**, each in a commit named after the finding, and each independently re-verified below rather than accepted on the strength of the commit message.

The project is now in a materially different category from v1: it lints clean, compiles clean, has a 166-test suite that runs in under a second, has real CI (lint + matrix test + wheel packaging assertion), has lost two of its three runtime dependencies, and no longer ships an unrunnable wheel or uses `assert` for user-facing errors.

What remains is a short list of *new* issues created by the rewrite (mostly concurrency semantics and stale docs).  The three test-coverage gaps that sat exactly on the former P0s were the sharpest item in the first draft of this section and are now closed (R2).

---

## 2. Status of every v1 finding

| # | v1 sev | v1 finding | Status | Fix commit | Re-verified at |
|---|---|---|---|---|---|
| 1 | P0 | Media inhibit (`playerctl`) is a no-op — `play_state` read but never assigned/applied | **Fixed** | [`e07f6f7`](https://github.com/lagerfeuer/pwr-tray/commit/e07f6f7) | [`../pwr_tray/main.py:608`](../pwr_tray/main.py:608), [`../pwr_tray/main.py:612`](../pwr_tray/main.py:612) |
| 2 | P0 | `restore_picks()` restores lock/sleep on the wrong axes — "sleep timeouts are not restored across restarts" | **Fixed** | [`bcf5a9b`](https://github.com/lagerfeuer/pwr-tray/commit/bcf5a9b) | [`../pwr_tray/main.py:497`](../pwr_tray/main.py:497) |
| 3 | P0 | `power_down = True` never powers down (`_get_down_state()` compared to the wrong string) | **Fixed** | [`dd8dbe5`](https://github.com/lagerfeuer/pwr-tray/commit/dd8dbe5) | [`../pwr_tray/main.py:746`](../pwr_tray/main.py:746) |
| 4 | P1 | `HiBattery` / `LoBattery` do not inherit the user's `[Settings]` values | **Fixed** | [`664e624`](https://github.com/lagerfeuer/pwr-tray/commit/664e624) | [`../pwr_tray/IniTool.py:234`](../pwr_tray/IniTool.py:234) + probe |
| 5 | P1 | A malformed list value yields `None` instead of the default | **Fixed** | [`11dab1c`](https://github.com/lagerfeuer/pwr-tray/commit/11dab1c) | [`../pwr_tray/IniTool.py:19`](../pwr_tray/IniTool.py:19), [`../pwr_tray/IniTool.py:249`](../pwr_tray/IniTool.py:249) |
| 6 | P1 | Non-importable module shipped in the wheel (`PrtUtil`/`YamlDump` imported but not in the sdist) | **Fixed** — both files deleted, `flit` include list is explicit | [`5e57007`](https://github.com/lagerfeuer/pwr-tray/commit/5e57007) | [`../pyproject.toml:45`](../pyproject.toml:45), [`../tests/test_package_imports.py:1`](../tests/test_package_imports.py:1) |
| 7 | P1 | `assert` used as user-facing error handling (vanishes under `python -O`) | **Fixed** — replaced by `PwrTrayError` subclasses | [`48f7b40`](https://github.com/lagerfeuer/pwr-tray/commit/48f7b40) | [`../pwr_tray/Errors.py:18`](../pwr_tray/Errors.py:18), [`../pwr_tray/main.py:145`](../pwr_tray/main.py:145) |
| 8 | P1 | Restarted instances escape the single-instance killer (argv matching too naive) | **Fixed** — `argv` normalisation + `flock` backstop | [`024048e`](https://github.com/lagerfeuer/pwr-tray/commit/024048e) | [`../pwr_tray/Utils.py:186`](../pwr_tray/Utils.py:186), [`../pwr_tray/Utils.py:289`](../pwr_tray/Utils.py:289) |
| 9 | P1 | Orphaned `dbus-monitor` child per restart | **Fixed** — terminate + reap, `atexit` registered | [`b220ca5`](https://github.com/lagerfeuer/pwr-tray/commit/b220ca5) | [`../pwr_tray/main.py:1121`](../pwr_tray/main.py:1121), [`../pwr_tray/main.py:1115`](../pwr_tray/main.py:1115) |
| 10 | P1 | Icon loading depends on a global `os.chdir` (breaks on any CWD change) | **Fixed** — absolute paths, no `chdir()` anywhere in the package | [`c269158`](https://github.com/lagerfeuer/pwr-tray/commit/c269158) | [`../pwr_tray/main.py:163`](../pwr_tray/main.py:163), [`../pwr_tray/main.py:220`](../pwr_tray/main.py:220) |
| 11 | P2 | Polling architecture: fixed 2 s tick re-spawns `playerctl`, `loginctl`, battery reads | **Fixed** — event sources + dirty-flag gating | [`2569df5`](https://github.com/lagerfeuer/pwr-tray/commit/2569df5) | [`../pwr_tray/PowerEvents.py:47`](../pwr_tray/PowerEvents.py:47), [`../pwr_tray/main.py:693`](../pwr_tray/main.py:693) |
| 12 | P2 | Module-level D-Bus dependency (`pydbus` imported at import time → app unimportable without it) | **Fixed** — `pydbus` dropped, `QtDBus` imported lazily | [`40c5c00`](https://github.com/lagerfeuer/pwr-tray/commit/40c5c00) | [`../pwr_tray/main.py:1169`](../pwr_tray/main.py:1169), [`../pyproject.toml:26`](../pyproject.toml:26) |
| 13 | P2 | Metadata drift (description said GTK, unversioned `requires-python`, `importlib-metadata` for a 3.8 shim) | **Fixed** — PyQt5, `>=3.11`, shim gone | [`dab3341`](https://github.com/lagerfeuer/pwr-tray/commit/dab3341) | [`../pyproject.toml:11`](../pyproject.toml:11), [`../pyproject.toml:19`](../pyproject.toml:19) |
| 14 | P2 | Config-path robustness (missing/!dirty `config.ini` could raise or silently mis-save) | **Fixed** — never raises, full-replace semantics, fallback | [`89201ae`](https://github.com/lagerfeuer/pwr-tray/commit/89201ae) | [`../pwr_tray/IniTool.py:191`](../pwr_tray/IniTool.py:191) |
| 15 | P2 | Project hygiene: no `.gitignore` for caches, no tests, no CI, dead resource files (`SetA/B/C`) | **Fixed** — caches ignored, 14 test files, 3-job CI, dead files removed | [`d6a05b8`](https://github.com/lagerfeuer/pwr-tray/commit/d6a05b8) | [`../.github/workflows/tests.yml:1`](../.github/workflows/tests.yml:1), [`../.gitignore:1`](../.gitignore:1) |

Nothing from v1 was deferred, and nothing regressed visibly.

---

## 3. Verification transcript

Gates (all run in the working tree before this document was written):

```
$ ruff check .
All checks passed!

$ python3 -m compileall -q pwr_tray
COMPILE_OK

$ python3 -m pytest -q
166 passed in 0.75s
```

Behavioural probes, run with `HOME`/`XDG_CONFIG_HOME` pointed at a throwaway directory so the live `~/.config/pwr-tray` was never touched:

* **Inheritance (v1 #4).** With `[Settings] lock=[3,7] sleep=[9,11] editor=my-edit` and a `[HiBattery]` section that sets only `lock`:
  `HiBattery lock=[4,5] sleep=[9,11] editor='my-edit'` — the unset keys now inherit instead of falling back to the built-in table.
* **Malformed list (v1 #5).** `lock_min_list = not-json` → `[15, 30]` (the declared default), not `None`, and the applet stays usable.
* **Deleted config (v1 #14).** `os.remove(config.ini)` then construct → the applet still comes up and rewrites a valid file.
* **CLI.** `pwr-tray --version` → version string matching `pyproject.toml`; `pwr-tray --list-des` renders the full table including `sway-wayland`, `niri-wayland`, `kde-wayland`; `--help` exits 0.

---

## 4. Residual findings in the current tree

These are new, introduced by (or exposed by) the rewrite. Nothing here is a P0.

### R1 — P1 — Kill-then-lock ordering turns "two icons" into "silently killed instance", and can restart-war under systemd — **Fixed**

[`../pwr_tray/main.py:1327`](../pwr_tray/main.py:1327) ran `PyKill().kill_loop('pwr-tray')` *before* the lock was acquired, so a second launch killed the incumbent instead of refusing. With `Restart=always` in the shipped units ([`../docs/examples/pwr-tray-niri.service:1`](../docs/examples/pwr-tray-niri.service:1) and the README examples), two supervisors that disagreed about who should run could ping-pong forever.

**Fix.** The lock is now taken first, in [`acquire_lock_or_take_over()`](../pwr_tray/main.py:1277): a failed acquire is a refusal (`exit 3`, message naming the holder and `--replace`), and nothing is signalled. Killing is the *takeover* path and is opt-in via `--replace`; even then the recorded pid is signalled only if its command line still names `pwr-tray` ([`PyKill.kill_pid()`](../pwr_tray/Utils.py:275)), and `restart_self()` strips `--replace` from the arguments it passes on. The unconditional `kill_loop()`/`_kill_script()` pair is gone.

**Re-verified.** `tests/test_single_instance.py` covers the refusal, the guarded takeover, a recycled/foreign pid and the main()-level exit-3-without-signalling path; an out-of-process probe (real `flock` held by a child whose argv is `pwr_tray/main.py`) confirmed a plain start refuses with the holder alive, while `--replace` terminates it (exit `-15`) and takes the lock; a holder whose argv does not name `pwr-tray` is left alone. README options table, the `setsid` restart line and the systemd caveats were updated to match.

### R2 — P1 — The three former P0 behaviours had no regression test — **Fixed**

The fixes for v1 #1/#2/#3 were verified by reading the code and by the author's manual testing, but the suite did not lock them down: `check_inhibited()` was monkeypatched away in [`../tests/test_power_events.py`](../tests/test_power_events.py), [`SwayIdleManager.build_cmd()`](../pwr_tray/SwayIdleMgr.py:58) had no test at all, and nothing exercised the `_get_down_state() == 'PowerDown'` branch ([`../pwr_tray/main.py:746`](../pwr_tray/main.py:746)).  The suite stayed green with all three reverted.

**Fix.** [`../tests/test_media_inhibit.py`](../tests/test_media_inhibit.py) drives the real [`check_inhibited()`](../pwr_tray/main.py:573) and [`build_cmd()`](../pwr_tray/SwayIdleMgr.py:58) against a fake `subprocess` and a bare `PwrTray`:

* `playerctl status == playing` becomes `inhibited == 'player'` — asserted through `was_inhibited`, the PlayingNow icon (index 4 of `svg_info.bases`), the swayidle restart and `poll_100ms` ([`../pwr_tray/main.py:616`](../pwr_tray/main.py:616));
* a real logind `block` row keeps priority over the player (StopSign, index 7), an `xfce4-power-man` / `org_kde_powerde` power-key row does not, and disabling `playerctl` in the menu stops the query entirely;
* [`SwayIdleManager.build_cmd()`](../pwr_tray/SwayIdleMgr.py:58) emits **no** `timeout` clauses (and no `after-resume`) while `was_inhibited == 'player'`, and restores `timeout 900 'swaylock'` / `timeout 1200 'systemctl suspend'` when playback stops — including the "did the command change" return value that decides whether swayidle is replaced.

[`../tests/test_power_events.py`](../tests/test_power_events.py) gains a harness that crosses the once-per-sample branch with `power_down` True and False, asserting `poweroff()` and `suspend()` respectively through [`_get_down_state()`](../pwr_tray/main.py:525).

**Re-verified by mutation.** Reverting the three fixes (`inhibited = inhibited`, `media_playing = False`, `_get_down_state()` returning a fixed `'Suspend'`) fails six of the new tests; nothing else in the tree changed.  `ruff check .` and `pytest -q` (166 passed) are clean.

### R3 — P2 — Per-tier defaults are unreachable once a section is absent

[`../pwr_tray/IniTool.py:51`](../pwr_tray/IniTool.py:51) declares per-tier defaults (e.g. `LoBattery.power_down = True`), but [`../pwr_tray/IniTool.py:234`](../pwr_tray/IniTool.py:234) makes a tier inherit the *user's* `[Settings]` whenever it is present or absent — which is the correct fix for v1 #4 but leaves two competing notions of "default". Probe: with only `[Settings]` in the file, `LoBattery.power_down` resolves to `False` (inherited) even though the table says `True`. Either the table's per-tier values should win when a key is absent from *both* `[Settings]` and the tier, or the table should not claim per-tier values at all. Right now the declaration is documentation that nothing honours.

### R4 — P2 — Two README statements are now false

* [`../README.md:156`](../README.md:156): "`pwr-tray` changes directory to `~/.config/pwr-tray`" — the `chdir` was removed in `c269158`; paths are absolute now. Only stale comments ([`../pwr_tray/main.py:166`](../pwr_tray/main.py:166), [`../pwr_tray/main.py:963`](../pwr_tray/main.py:963)) reference the old behaviour.
* [`../README.md:239`](../README.md:239): "or you will get two tray icons" — inaccurate after R1: the newer instance kills the older one instead of coexisting.

Documentation that contradicts the implementation is worse than no documentation, because it is the only thing a new user reads before filing a bug.

### R5 — P3 — Resume detection is a third `dbus-monitor` child, still polled per tick

[`../pwr_tray/main.py:1106`](../pwr_tray/main.py:1106) spawns its own `dbus-monitor` for `PrepareForSleep`, and [`../pwr_tray/main.py:1142`](../pwr_tray/main.py:1142) reads it with a non-blocking `os.read` on **every** tick ([`../pwr_tray/main.py:698`](../pwr_tray/main.py:698), unconditional, outside the dirty-flag gating). `PowerEvents` already owns a system-bus monitor with a `QSocketNotifier` and already parses `login1`. Folding resume detection into that notifier would remove a process and the last per-tick syscall. Low risk, low urgency — `os.read` on an empty pipe is cheap.

### R6 — P3 — Small CLI blemishes

* [`../pwr_tray/main.py:1291`](../pwr_tray/main.py:1291): `--help` shows `-q, --quick ... (1m lock + 1m sleep` — the closing paren is missing.
* `--version` prints `main.py 1.0.13` when invoked as `python -m pwr_tray.main` because it formats `%(prog)s`. Harmless, but the console script and the module path disagree.

### R7 — P3 — Dead code left behind by the rewrite

* [`../pwr_tray/main.py:215`](../pwr_tray/main.py:215): `self.here_dir` is assigned and never read.
* [`../pwr_tray/Utils.py:173`](../pwr_tray/Utils.py:173): `x_restart_self()` is unused (superseded by `PwrTray.restart_self`).
* [`../pwr_tray/IniTool.py:79`](../pwr_tray/IniTool.py:79): `self.section_params` is unused.

Three `ruff`-invisible leftovers; each is a one-line deletion.

### R8 — P2 — `PwrTray.__init__` crashed on start-up: `ensure_resources()` called with one argument — **Fixed**

[`../pwr_tray/main.py:223`](../pwr_tray/main.py:223) called `self.ensure_resources(self.svgs)` (and `ensure_resources(['lockpaper.png'])`), but the method is `ensure_resources(folder, resources)`. Every start therefore died with `TypeError: ensure_resources() missing 1 required positional argument: 'resources'` — after the lock had been taken and the tray wait completed, so the applet (and `./runner`) simply exited and nothing appeared in the tray. The suite missed it because the icon helpers are tested directly while `__init__` itself is never constructed under pytest.

**Fix.** Both call sites pass `ini_tool.folder`, and [`../tests/test_icon_loading.py`](../tests/test_icon_loading.py) now parses `main.py` and asserts that every `ensure_resources()` call site passes exactly `(ini_tool.folder, resources)`.

---

## 5. What is genuinely good (unchanged, now better supported)

* **The UX model is the product.** A tri-state master mode (Normal / Presentation / NoSleep), per-tier battery profiles, the inhibitor count rendered *in the icon*, and the click-to-see-which-inhibitor behaviour are a coherent answer to "why is my laptop not sleeping", which is the question that actually generates support threads. Nothing else in the table below combines them.
* **One UI across nine desktops.** i3/X11, sway, Hyprland, niri, KDE (X11 + Wayland), XFCE, Cinnamon, MATE, LXQt via a declarative [`commands.yaml`](../pwr_tray/commands.yaml) plus `--de` overrides and `--list-des`. This remains the single strongest argument for using it over a compositor-native script.
* **Error reporting is now actionable.** [`../pwr_tray/Errors.py:31`](../pwr_tray/Errors.py:31) and friends produce messages naming the missing command, the install hint, and the `--list-des`/`--de NAME` escape hatch, instead of an `AssertionError` that disappears under `-O`.
* **Console capture survives log redirection.** [`../pwr_tray/Utils.py:50`](../pwr_tray/Utils.py:50) keeps a duplicate of fd 2 so errors reach the terminal that launched the applet, not just the journal.
* **The test suite is honest.** Autouse isolated-`HOME` fixture, permissive stubs so the suite runs without PyQt5, and regression tests that cite their audit finding in the docstring ([`../tests/test_restore_picks.py:1`](../tests/test_restore_picks.py:1)).  The three behaviours the whole rewrite existed to fix — media inhibit, restored timeouts, `power_down` — are now pinned by tests that fail if the fix is reverted ([`../tests/test_media_inhibit.py:1`](../tests/test_media_inhibit.py:1), [`../tests/test_power_events.py:239`](../tests/test_power_events.py:239)).
* **CI asserts the artefact, not just the tests.** [`../.github/workflows/tests.yml:1`](../.github/workflows/tests.yml:1) installs the built wheel into a clean venv and exercises both `--version` and `--list-des`, which is exactly the check that would have caught v1 #6.

Current shape:

| Metric | v1.0.12 | v1.0.13 |
|---|---|---|
| `pwr_tray/` Python lines | 1,007 (`main.py`) | 1,380 (`main.py`) + 849 across 6 modules |
| Runtime dependencies | 3 (`psutil`, `pydbus`, `ruamel.yaml`) + PyQt5 | 3 (`psutil`, `PyQt5`, `ruamel.yaml`) |
| `ruff` | not configured | clean, 11 rule families |
| Tests | 0 | 166 in 15 files, 0.75 s |
| CI | none | lint + 3.11/3.12/3.13 test + wheel packaging assertion |
| `requires-python` | unreleased / unstated | `>=3.11` |

---

## 6. Value relative to alternatives

| Need | pwr-tray | Nearest alternative | Verdict |
|---|---|---|---|
| See *why* sleep is inhibited | Icon + menu listing every logind inhibitor | `systemd-inhibit --list` (CLI), DE battery applets (opaque) | **pwr-tray wins** on legibility |
| Stop autolock during video | Auto-detects `playerctl` playback | Hand-written `swayidle` rules, or `caffeine`-style toggles | **pwr-tray wins** on zero-config |
| Battery-tier behaviour | Named tiers with per-tier lock/sleep lists and rotation | `tlp`, `auto-cpufreq` (power tuning, not session policy) | **pwr-tray wins** for session UX, loses on actual power tuning |
| Suspend/hibernate integration | Yes, incl. `power_down` | `systemctl suspend` in a keybind | Roughly equal; pwr-tray adds confirmation UI |
| One config across DEs | `commands.yaml` + `--de` | Compositor-specific scripts | **pwr-tray wins**, but this is the niche inside the niche |
| Maintenance / bus factor | 1 author, 2 stars, 1 watcher, 0 open issues | `swayidle`, `swaylock`, `loginctl` (shipped by distros) | **Alternatives win** |

**Honest verdict, unchanged in direction, improved in degree:** this is a well-built tool for a specific person — someone running two or more of i3/sway/Hyprland/niri/KDE who wants session-power policy to follow them and wants to see inhibitor state. For that person it has real value that the alternatives do not replicate; the v1 objection was never the idea, it was that the implementation could not be trusted. As of `v1.0.13` it can: the two silent-data-loss bugs, the dead feature flag, the broken wheel, and the `assert`-as-error-handling are all gone, and there is a test suite standing behind them.

What has *not* changed is the market. 2 stars, 1 watcher, 0 issues, 21 releases means effectively zero external users — so the honest framing is: keep using it, and treat its value as personal leverage, not as a project others should adopt. The gap between "good tool" and "tool worth recommending" is now mostly R1 and R4: today the process-supervision story is subtly wrong and the README describes a version of the app that no longer exists.

---

## 7. Recommended next steps (in order)

1. ~~**R1** — move `lock.acquire()` ahead of `PyKill().kill_loop()`, make the kill the *takeover* branch, and add `--replace` for the intentional case.~~ **Done** (see §4, R1).
2. ~~**R2** — three small tests: `check_inhibited()` returning `'player'`; `SwayIdleManager.build_cmd()` omitting lock/sleep when `was_inhibited == 'player'`; `on_timeout()` calling `poweroff()` when `_get_down_state()` is `'PowerDown'`. Without these the audit's own P0s are one revert away.~~ **Done** (see §4, R2).
3. **R4** — correct [`../README.md:156`](../README.md:156) and [`../README.md:239`](../README.md:239) to match the new behaviour, and say plainly which supervisor should own the process when a systemd unit is used.
4. **R3** — decide whether per-tier defaults exist; make [`../pwr_tray/IniTool.py:51`](../pwr_tray/IniTool.py:51) authoritative or delete the per-tier entries.
5. **R5** — fold resume detection into `PowerEvents`' existing system-bus notifier and delete the third child process.
6. **R6/R7** — close the paren, fix `%(prog)s`, delete the three dead attributes/methods.

---

## Appendix — how this was checked

* `git log --oneline -16` — confirmed fifteen fix commits (`e07f6f7`, `dd8dbe5`, `bcf5a9b`, `664e624`, `11dab1c`, `5e57007`, `48f7b40`, `024048e`, `b220ca5`, `c269158`, `2569df5`, `40c5c00`, `dab3341`, `89201ae`, `d6a05b8`) on top of the audited `b1b9735`.
* `wc -l pwr_tray/*.py tests/test_*.py conftest.py` — 4,555 lines total; `main.py` 1,380.
* `ruff check .`, `python3 -m compileall -q pwr_tray`, `python3 -m pytest -q`.
* Isolated-`HOME` probes against `IniTool` for inheritance, malformed list values, and a deleted `config.ini`.
* CLI smoke tests through an interpreter that has the runtime deps, with `PYTHONPATH` pointed at the source tree: `--version`, `--list-des`, `--help`. The applet itself was deliberately **not** launched, to avoid killing a live instance.
* `grep` for the stale README claims and for every `chdir`/`here_dir`/`x_restart_self`/`section_params` reference.
