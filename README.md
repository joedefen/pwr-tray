# pwr-tray

`pwr-tray` is a GTK5 Tray Applet for Power/Energy Saving and System/DE Controls; currently supported/tested DEs are: i3wm, swaywm, and KDE on X11. Its menu will look similar to:

<p align="center">
  <img src="https://github.com/joedefen/pwr-tray/blob/main/images/pwr-tray-screenshot.png?raw=true" alt="screenshot">
</p>


With a right-click and a left-click, you can do operations such as change to Presentation Mode, lock your screen, blank your monitors, change screen lock and sleep timeouts, lock and blank your monitors, and more. The `pwr-tray` icon changes based on state:

* <img src="https://github.com/joedefen/pwr-tray/blob/main/src/pwr_tray/resources/FullSun-v03.svg?raw=true" alt="FullSun" width="24" height="24"> Presentation Mode (i.e., the full sun)
* <img src="https://github.com/joedefen/pwr-tray/blob/main/src/pwr_tray/resources/SettingSun-v03.svg?raw=true" alt="SettingSun" width="24" height="24"> SleepAfterLock Mode (i.e., the setting sun)
    * <img src="https://github.com/joedefen/pwr-tray/blob/main/src/pwr_tray/resources/RisingMoon-v03.svg?raw=true" alt="RisingMoon" width="24" height="24"> SleepAfterLock Mode and Locking Screen Soon
* <img src="https://github.com/joedefen/pwr-tray/blob/main/src/pwr_tray/resources/Unlocked-v03.svg?raw=true" alt="Unlocked" width="24" height="24"> LockOnly Mode  (i.e., the unlocked lock)
    * <img src="https://github.com/joedefen/pwr-tray/blob/main/src/pwr_tray/resources/UnlockedMoon-v03.svg?raw=true" alt="UnlockedMoon" width="24" height="24"> LockOnly Mode and Locking Screen Soon
* <img src="https://github.com/joedefen/pwr-tray/blob/main/src/pwr_tray/resources/GoingDown-v03.svg?raw=true" alt="GoingDown" width="24" height="24"> LowBattery State (going down).
* <img src="https://github.com/joedefen/pwr-tray/blob/main/src/pwr_tray/resources/PlayingNow-v03.svg?raw=true" alt="PlayingNow" width="24" height="24"> Inhibited by playing media.
* <img src="https://github.com/joedefen/pwr-tray/blob/main/src/pwr_tray/resources/StopSign-v03.svg?raw=true" alt="StopSign" width="24" height="24"> Inhibited by systemd inhibitors.

---

### HowTo Install
* Basically: `pipx install pwr-tray` (exactly how depends on your installation and its state)
* Manually run as `pwr-tray -o`:
    * Creates config (in `~/.config/pwr-tray/config.ini`).
    * Shows missing system-level commands (DE dependent).
      * `systemctl` is always required.
      * Optionally, install `playerctl` if you wish playing media to inhibit screen saving and sleeping.
* Then, follow the "Per-DE Specific Notes" below to ensure proper operation. To just kick the tires, you can defer this until ready to go forward.
* Finally, read the other sections for customization and everyday use.

---

## HowTo Run pwr-tray

### Manual Launch of the Tray Applet
- For foreground in terminal, run `pwr-tray -o` ("-o" logs to stdout).
- In the background, run `setsid pwr-tray &` (logs to `~/.config/pwr-tray/debug.log`).
### Other Forms of pwr-tray (for Maintenance/Debugging)
- `pwr-tray -e` edits the config file (`~/.config/pwr-ini/config.ini`)
- `pwr-tray -f` tails the log file (`~/.config/pwr-ini/debug.log`)

### Initial Testing of pwr-tray
- Running `pwr-tray --quick` reduces the lock and sleep timeout to 1 minute (although you can 'click' the current value to try others), and `--quick` runs double-time (so 1 minute timers expire in 30s per the wall clock).
- You can run in various modes, but the default, `SleepAfterLock`, exercises the most code paths.
- Then, ensure closing the lid, hitting the power button, etc., have the desired effects.

## More Testing Hints
* To test systemd inhibits: create a test inhibit with `systemd-inhibit --why="Prevent sleep for demonstration" sleep infinity`
* To test Hi/Lo Battery states: when plugged in, click the battery state which artificially changes to HiBattery or LoBattery states for testing behaviors in those states.

---

### HowTo Configure pwr-tray
- When the program is started w/o a `config.ini`, that file is created with defaults.
- It has three sections:
    * **Settings**: The settings for when plugged in.  Missing/invalid settings are inherited from the defaults. Here are the defaults:
    * **HiBattery**: The settings for when on battery on and not a low battery condition.  Missing/invalid settings are inherited from 'Settings'.
    * **LoBattery**: The settings for when on battery in a low battery condition.  Missing/invalid settings are inherited from 'Settings'.

Here are the current 'Settings' defaults with explanation.
```
[Settings]
i3lock_args = -t -i ./lockpaper.png # arguments when running i3lock
debug_mode = False                  # more frequent and elaborate logging
power_down = False                  # power down (rather than suspend)
turn_off_monitors = False           # turn off monitors after locking screen
lock_min_list = [15, 30]            # lock minutes choices
sleep_min_list = [5, 30]            # sleep minutes choices (after lock)
lo_battery_pct = 10                 # define "low battery" state
gui_editor = geany                  # gui editor for .ini file
```
**NOTES**:
* If you have issues with monitors failing to sleep or the system cannot wake when the monitors are off, then disable the `turn_off_monitors` feature.
* You can set `gui_editor = konsole -e vim`, for example, to use vim in a terminal window.  If you don't have `geany` installed, then be sure to change `gui_editor`.
* `pwr-tray` changes directory to `~/.config/pwr-tray`.
* If its .ini file is missing, it is created and `lockpaper.png` is copied there too.
* Your picks of mode, timeouts, etc. are saved to disk when changed, and restored on the next start.
* Items may be absent depending on the mode and battery state.
- **NOTE**: when in LoBattery, SleepAfterLock becomes the effective mode. The icon will change per your selection and the battery state.

---

### HowTo Configure pwr-tray
Open the menu with a right- or left-click. Then click a line to have an effect:

Choose from three *major power modes* (to control the effects of timeouts):
- **🅟 Presentation ⮜** -  Keeps the screen unlocked/on and system up.
- **🅛 LockOnly ⮜** - Keeps the system up, but the screen may lock.
- **🅢 SleepAfterLock ⮜** - Allows screen locking and system to go down (the "normal" mode).

Next, you may choose from various locking/blanking/DE operations:
- **▷ Lock Screen** - locks the screen immediately.
- **▷ Blank Monitors** - blanks the screen after locking the screen.
- **▷ Reload i3** - various DE-dependent actions.
- **▷ Log Off** - terminate your user session.

Or choose a new *system state*:
- **▼ Suspend System** - suspends the system immediately.
- **▼ Reboot System** - reboots the system immediately.
- **▼ Poweroff System** - power down the system immediately.

Next, you may see:
- **🗲 Plugged In** (or HiBattery or LoBattery). Shows major state of the battery.
- **♺ Chg Screen Idle: 15m->30m** - change the time to start the screen saver; each time clicked, it changes to the next choice.
- **♺ Chg System Idle: 5m->30m** - change the time to take the system down; clicking selects the next choice.


Or act on the applet itself:
- **🎝 PlayerCtl** - shows the state (not installed, enabled, disabled); if installed, a click toggles whether playing media inhibits screen locking and sleeping.
- **🖹  Edit Applet Config** - edit the applet's .ini file.
- **☓ Quit this Applet** -  exit applet.
- **↺ Restart this Applet** - restart applet.

---

## Per-DE Specific Notes

### i3wm Specific Notes
* Uninstall or disable all competing energy saving programs (e.g., `xscreensaver`, `xfce4-power-manager`, etc.) when running `i3` whether started by `systemd` or `i3/config` or whatever; defeat the X11 defaults somehow such as in `~/.config/i3/config`:
```
        exec --no-startup-id xset s off ; xset s noblank ; xset -dpms
```
* Edit `/etc/systemd/logind.conf` and uncomment `HandlePowerKey=` and `HandleLidSwitch=`, set each action to `suspend`, and then either reboot or restart `systemd-logind`.  That enables `xss-lock` to handle those keys.
* Finally, start your pwr-tray somehow. Below is a simplest case using `i3status`, but it may depend on your status bar:
```
        bar { 
            status_command i3status
            tray_output primary
        }
        exec_always --no-startup-id ~/.local/bin/pwr-tray
```
* If you use `polybar` for status, then it may be best to run `pwr-tray` from polybar's 'launch' script; e.g., `sleep 1.5 && setsid ~/.local/bin/pwr-tray &`;  the delay may be need to allow time for the tray to become ready.

### sway Specific Notes
* Uninstall or disable all competing energy saving programs (e.g., `swayidle`, `xfce4-power-manager`, etc.) when running `sway` whether started by `systemd` or `sway/config` or whatever.
* **NOTE**: on `sway`, `pwr-tray` cannot read the idle time and do its usual micromanagement; instead, it runs a `swayidle` command whose arguments may vary with your settings.
* Edit `/etc/system/logind.conf` and uncomment `HandlePowerKey=` and `HandleLidSwitch=`, and set each action to `suspend`; then either reboot or restart `systemd-logind`.  That enables the ever-running `swayidle` to handle the suspend / resume events.
* Again, find a way to start `pwr-tray`; perhaps adding to sway's config: `exec_always --no-startup-id sleep 2 && ~/.local/bin/pwr-tray`; a delay may be required to let the tray initialize.

### KDE (X11) Specific Notes
* In Settings/Energy Saving, disable "Screen Energy Saving", "Suspend session", etc., except keep the "Button events handling" and make it as you wish (e.g., "When power button pressed", "Sleep").
* In Settings/AutoStart, add the full path of `~/.local/bin/pwr-tray`.

