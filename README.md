# pwr-tray
A GTK Tray Applet for Power/Energy Saving and System/DE Controls 

### Install
- Install prerequisites; see top of `gtk-power-app`
    - Run by hand in terminal, and check that actions work
- Copy `gtk-power-app` to ~/.local/bin
- Add to autostart

### Manual Launch
- For foreground in terminal, run `gtk-power-app`
- In background, run `setsid gtk-power-app >/dev/null 2>&1 &`

### Output for Debugging 
- unless stdout is a tty, say for debugging, ~/.gtk-power-app.log is used for stdout
- if we create a log file, its size is limited to 512K and then it is truncated

### Menu Options
Choose from three *major power modes* (to limit the effect of timeouts):
- **☀ PRESENTATION Mode** -  Keeps the monitors on and system up.
- **☀ NO-SLEEP Mode** - Keeps the system up, but not the monitors on.
- **☀ NORMAL Mode** - Allows the monitors off and system to go down (the default).

Or choose to *start the screen saver / lock*:
- **▷ Start Screensaver** - start the screensaver (configure your screensaver separately, as you wish, but it should not be "None").
- **▷ Blank Monitors** - Blanks the screen immediately and locks the screen (using your screen saver).

Or choose a new *system state*:
- **▼ Suspend System** - suspends the system immediately.
- **▼ Reboot System** - reboots the system immediately.
- **▼ Poweroff System** - power down the system immediately.

Or choose new controls values: 
- **♺ Chg Screen Idle: 10m->30m** - change the time to start the screen saver; each time clicked, it changes to the next one; you can fix the timeout or choose a list of them from the command line options; the default list is [10m, 30m].
- **♺ Chg System Idle: 30m->60m** - change the time to take the system down; clicking selects the next value; change on the command line; the default is [30m, 60m].
- **♺ Chg Down State: Suspend** - toggle how the system goes down when System Idle expires, Suspend or PowerOff;  you can select the default from the command line.

**Notes:**
- only the menu options that can have effect are shown (e.g., you cannot see "Presentation Mode" if in "Presentation Mode", you cannot see "Chg Screen Idle" if there is only one possibility, etc).
