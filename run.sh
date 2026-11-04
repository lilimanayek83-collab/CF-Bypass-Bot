#!/usr/bin/env bash
pkill -f "python.*bot.py" 2>/dev/null; pkill -f chrome 2>/dev/null
rm -f cfdl.session.lock
exec xvfb-run -a python3 bot.py
