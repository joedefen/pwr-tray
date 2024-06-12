# pwr-tray
A GTK Tray Applet for Power/Energy Saving and System/DE Controls.

Currently supported/tested DEs are:
* i3wm
* KDE on X11

In the plans are:
* Support for these DEs:
    * swaywm
    * KDE on Wayland
    * Gnome on X11
    * Gnome on Wayland
* And, imaginably, an AppImage that bundles all the system requirements since pip/pipx
does not deal with those.

---

### Install
* Basically: `pipx install pwr-tray` (exactly how depends on your installation and its state)
* Plus, various system modules and executables.
* Manually run as `pwr-tray -o`:
    * Creates config (in `~/.config/pwr-tray/config.ini`).
    * Shows missing, required system-level imports (DE dependent).
        * See "Installing Needed System Python Modules" (immediately below).
    * Shows missing system-level commands (DE dependent). Install per distro instructions.
      `systemctl` and `playerctl` are always required.
* When running manually, see the "Per-Distro Specific Notes" below for its configuration.
* Finally, see the "Using pwr-tray" for everyday use after install.

#### Installing Needed System Python Modules
* On Debian, use `sudo apt install ...` of:
    * python3-gi python3-gi-cairo gir1.2-gtk-3.0
    * gir1.2-appindicator3-0.1 libappindicator3-1
* On Debian 12 and others?, if the above is not working, try:
    * python3-gi python3-gi-cairo gir1.2-gtk-3.0
    * gir1.2-notify-0.7 gir1.2-ayatanaappindicator3-0.1 ayatanalibappindicator3-1
* On Arch, use `pacman -Syy ...` of:
    * libappindicator-gtk3 python-gobject python-cairo
* On other distros, use the hints above to resolve any missing "imports".

---

## Using pwr-tray

### Manual Launch of the Applet
- For foreground in terminal, run `pwr-tray -o` ("-o" logs to stdout).
- In the background, run `setsid pwr-tray &` (logs to `~/.config/pwr-tray/debug.log`).
### Other Forms of pwr-tray
- `pwr-tray -e` edits the config file (`~/.config/pwr-ini/config.ini`)
- `pwr-tray -f` tails the log file (`~/.config/pwr-ini/debug.log`)

### Tests
- Running `pwr-tray --quick` reduces the lock and sleep timeout to 1 minute
  (although you can 'click' the current value to try others),
  and `--quick` runs double-time (so 1 minute timers expire in 30s per the wall clock).
- You can run in various modes, but the default, SleepAfterLock, runs thru almost every action.
- Then ensure closing the lid, hitting the power button, etc., has the desired effects.

---

# Configuration Basics
- When the program is started w/o a `config.ini`, then it is created with defaults.
- It has three sections:
    * **Settings**: The settings for when plugged in.  Missing/invalid settings are inherited from the defaults. Here are the defaults:
    * **HiBattery**: The settings for when on battery on and not a low battery condition.  Missing/invalid settings are inherited from 'Settings'.
    * **LoBattery**: The settings for when on battery in a low battery condition.  Missing/invalid settings are inherited from 'Settings'.

Here are the current 'Settings' defaults with explanation.
```
[Settings]
i3lock_args = -t -i ./lockpaper.png # arguments when running i3lock
debug_mode = False                  # more frequent and elaborate logging
power_down = False                  # power down (rather than Suspend)
turn_off_monitors = False           # turn off monitors after locking screen
lock_min_list = [15, 30]            # lock minutes choices
sleep_min_list = [5, 30]            # sleep minutes choices (after lock)
lo_battery_pct = 10                 # define "low battery" state
```
NOTE: I've had problems with turning off the monitors, and if you do so, then
it is harder to know the system state. If you have issues with the monitors
failing to sleep or the system cannot wake with the monitors off, then
avoid those that "feature".

### Tray Menu 
Notes:
* `pwr-tray` changes directory to `~/.config/pwr-tray`.
* If .ini file is missing, it is created and `lockpaper.png` is copied there too.
* Your picks of mode and times are saved to disk when changed, and restored on the next start.
* Items may be absent depending on the mode and battery state.
* Icons change based on state:
    * **Full Sun**: Presentation Mode
    * **Setting Sun**: SleepAfterLock Mode ("normal")
        - **Setting Sun + Moon** : SleepAfterLock Mode and Locking Screen Soon
    * **Open Lock**: LockOnly Mode
        - **Open Lock + Moon** : LockOnly Mode and Locking Screen Soon
    * **Red Downward Triangle**: LowBattery State
    * **Musical Notes**: Inhibited when playing media or by other inhibitors.

First, you may see:
- **🗲 Plugged In** (or HiBattery or LoBattery). If you click this when you don't have a battery, then it switches to the next state.  This allows you to test battery state handly faily well and/or repurpose the battery config sections.
- **♺ Chg Screen Idle: 15m->30m** - change the time to start the screen saver; each time clicked, it changes to the next one.
- **♺ Chg System Idle: 5m->30m** - change the time to take the system down; clicking selects the next value.

Next, choose from three *major power modes* (to control the effects of timeouts):
- **⮀ PRESENTATION Mode** -  Keeps the screen unlocked/on and system up.
- **⮀ LockOnly Mode** - Keeps the system up, but the screens may lock.
- **⮀ SleepAfterLock Mode** - Allows screen locking and system to go down (the "normal" mode).
- **NOTE**: when in LoBattery, SleepAfterLock is in effect.
  The icon will change per your selection and the battery state.


Next, you may choose from various locking/blanking/DE operations:
- **▷ Lock Screen** - lock screen.
- **▷ Blank Monitors** - Blanks the screen after locking the screen.
- **▷ Reload i3** - various DE-dependent actions.
- **▷ Log Off** - terminate your user session.

Or choose a new *system state*:
- **▼ Suspend System** - suspends the system immediately.
- **▼ Reboot System** - reboots the system immediately.
- **▼ Poweroff System** - power down the system immediately.

Or act on the applet itself:
- **☓ Quit this Applet** -  exit applet.
- **↺ Restart this Applet** - restart applet.

---

## Per-Distro Specific Notes

### i3wm Specific Notes
* Uninstall or disable all competing energy saving programs (e.g., `xscreensaver`, `xfce4-power-manager`, etc.) when running `i3` whether started by `systemd` or `i3/config` or whatever; don't forget the X11 defaults that can be defeated many ways such as in in `~/.config/i3/config`:
```
        exec --no-startup-id xset s off ; xset s noblank ; xset -dpms
```
* Edit `/etc/system/logind.conf` and uncomment `HandlePowerKey=`, `HandlePowerKey=`, `HandlePowerKey=`, and `HandlePowerKey=`, and set the action to `suspend` (reboot or restart `systemd-logind`).  That enables `xss-lock` to handle those keys similar to `pwr-tray`.
* In `~/.config/i3/config`, configure `xss-lock` something like:
```
        set $screenlock i3lock -t -i ./lockpaper.png --ignore-empty-password --show-failed-attempts
        exec --no-startup-id xss-lock --transfer-sleep-lock -- $screenlock --nofork
```
* Finally, start your pwr-tray somehow. Below is a simplest case, but it may depend on your status bar:
```
        bar { 
            status_command i3status
            tray_output primary
        }
        exec_always --no-startup-id ~/.local/bin/pwr-tray
```
* If you use `polybar` for status, then it may be best to run `pwr-tray` from polybar's 'launch' script, and I had to run it as `env DISPLAY=:0 pwr-tray` and delay to ensure the tray is ready.

### sway Specific Notes
* Uninstall or disable all competing energy saving programs (e.g., `swayidle`, `xfce4-power-manager`, etc.) when running `sway` whether started by `systemd` or `sway/config` or whatever.
* NOTE: on `sway`, `pwr-tray` cannot read the title time and do its usual micromanagement of
  the system; instead, it runs a `swayidle` whose arguments may change with you change `pwr-tray`
  settings either in the .ini file or by clicking tray items.
* Edit `/etc/system/logind.conf` and uncomment `HandlePowerKey=`, `HandlePowerKey=`,
  `HandlePowerKey=`, and `HandlePowerKey=`, and set the action to `suspend`
  (reboot or restart `systemd-logind`).  That enables the ever-running `sway-idle` to handle
  the suspend / resume events.

### KDE (X11) Specific Notes
* In Settings/Energy Saving, disable "Screen Energy Saving", "Suspend session", etc., except keep the "Button events handling" and make it as you wish (e.g., "When power button pressed", "Sleep").
* In Settings/AutoStart, add the full path of `~/.local/bin/pwr-tray`.

