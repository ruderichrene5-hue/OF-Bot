# Server backup (08.11.2026, Hetzner 167.235.141.50)

Restore: clone to /root/adb_bot, create .venv, put secrets bundle back to /etc/adbbot/,
copy server_backup/systemd/* to /etc/systemd/system/, daemon-reload, enable only needed timers,
`crontab server_backup/crontab.txt`.
