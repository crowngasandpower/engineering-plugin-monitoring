#!/bin/sh
# Crown site ping — boot persistence script
# Place in /data/on_boot.d/99-site-ping.sh on each firewall.
# Reinstalls the cron job and hosts entry after every firmware update.
# The script itself (/data/site-ping.py) lives in /data/ which survives firmware updates.

chmod +x /data/site-ping.py
(crontab -l 2>/dev/null | grep -v site-ping; echo "* * * * * python3 /data/site-ping.py 2>/dev/null") | crontab -
