#!/bin/sh
# Starts as root only long enough to make the data folder writable, then runs Beacon as the unprivileged "beacon" user.
set -e
D="${DATA_DIR:-/data}"
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$D"
  find "$D" -not -user beacon -exec chown beacon:beacon {} + 2>/dev/null || echo "WARNING: could not change the owner of $D; if Beacon cannot save settings, make that folder writable for user ID 10001."
  exec gosu beacon "$@"
fi
exec "$@"
