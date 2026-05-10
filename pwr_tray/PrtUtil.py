#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Print Utility (to rotating log file)
   - NOTE: not being used (future considerations)
   - does not do the checking of where to log (stdout or file) the same
"""
# pylint: disable=invalid-name
import sys
import os
import logging
import inspect
from logging.handlers import RotatingFileHandler
from datetime import datetime
from io import StringIO

# --- Configuration ---
prt_path = os.path.expanduser("~/.config/pwr-tray/debug.log")
prt_kb = 512

# Initialize Logger
logger = logging.getLogger("pwr_tray_logger")
logger.setLevel(logging.INFO)
logger.propagate = False

# Ensure we don't add multiple handlers if the module is reloaded
if not logger.handlers:
    os.makedirs(os.path.dirname(prt_path), exist_ok=True)
    # backupCount=1 gives you debug.log.1
    rfh = RotatingFileHandler(
        prt_path,
        maxBytes=prt_kb * 1024,
        backupCount=1,
        encoding='utf-8'
    )
    # Simple format for the file: the message itself (we build the timestamp in prt)
    rfh.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(rfh)

def where(above=0):
    """ Figure out where we are called from """
    stack = inspect.stack()
    frameNo = 2 + above
    if frameNo < 0 or len(stack) < frameNo + 1:
        return '[n/a]'
    # stack[frameNo][1] is filename, [2] is line number
    filename = stack[frameNo][1]
    line_number = stack[frameNo][2]
    return f'[{filename.split("/")[-1]}:{line_number}]'

def prt(*args, **kwargs):
    """
    Custom print routine.
    - Uses RotatingFileHandler to prevent FD leaks.
    - Automatically captures caller location via where().
    """
    to_stdout = kwargs.pop('to_stdout', None)

    # Build the message
    dt = datetime.now().strftime('%m-%d^%H:%M:%S')

    # Capture print-style arguments into a string
    temp_buffer = StringIO()
    print(*args, file=temp_buffer, end='')

    # Combine everything: Timestamp + Message + File/Line
    log_line = f"{dt} {temp_buffer.getvalue()} {where()}"

    # Determine output destination
    # 1. Force stdout if requested
    # 2. Use stdout if running in a terminal (TTY)
    # 3. Otherwise, use the rotating logger (File)
    if to_stdout or (to_stdout is None and sys.stdout.isatty()):
        print(log_line, flush=True)
    else:
        logger.info(log_line)
