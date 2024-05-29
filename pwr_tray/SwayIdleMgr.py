#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import subprocess
import signal
import time

# Function to start swayidle with given arguments
def start_swayidle(args):
    command = ['swayidle'] + args
    process = subprocess.Popen(command)
    return process

# Function to kill a given process
def kill_swayidle(process):
    process.send_signal(signal.SIGTERM)
    process.wait()

# Function to check if a process is still running
def is_process_running(process):
    return process.poll() is None

# Example usage
if __name__ == "__main__":
    # Initial arguments for swayidle
    swayidle_args = [
        'timeout', '300', 'swaylock -f -c 000000',
        'timeout', '600', 'systemctl suspend',
        'resume', 'swaymsg "output * dpms on"'
    ]

    # Start swayidle with initial arguments
    swayidle_process = start_swayidle(swayidle_args)
    print(f'Started swayidle with PID {swayidle_process.pid}')

    try:
        # Monitor the swayidle process
        while True:
            time.sleep(5)  # Check every 5 seconds

            if not is_process_running(swayidle_process):
                print(f'swayidle with PID {swayidle_process.pid} has terminated.')
                break  # Exit the loop or handle the process termination as needed

            # Here you could add your condition to restart swayidle with new arguments
            # For example, if some external condition is met, kill and restart swayidle
            # ...

    finally:
        # Ensure that the swayidle process is killed on script exit
        if is_process_running(swayidle_process):
            print(f'Cleaning up: killing swayidle with PID {swayidle_process.pid}')
            kill_swayidle(swayidle_process)

