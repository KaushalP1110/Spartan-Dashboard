#!/bin/bash
set -e

# OpenDental's MySQL: every project connects to 127.0.0.1:3306 (their configs
# were written for the PC running OpenDental). Forward that port to the real
# database host so those configs work unchanged inside the container.
if [ -n "$OPENDENTAL_DB_HOST" ]; then
  echo "[spartan] Forwarding 127.0.0.1:3306 -> ${OPENDENTAL_DB_HOST}:${OPENDENTAL_DB_PORT:-3306} (OpenDental MySQL)"
  socat TCP-LISTEN:3306,fork,reuseaddr,bind=127.0.0.1 "TCP:${OPENDENTAL_DB_HOST}:${OPENDENTAL_DB_PORT:-3306}" &
fi

# Python dependencies of every mounted project, into a venv kept in a named
# volume (quick after the first start). pywinauto is Windows-only and not
# used at runtime any more, so it is skipped.
[ -x /venv/bin/python ] || python -m venv /venv
for req in /workspace/*/requirements.txt; do
  [ -f "$req" ] || continue
  echo "[spartan] Installing Python requirements: $req"
  grep -viE '^[[:space:]]*pywinauto' "$req" > /tmp/requirements.txt || true
  /venv/bin/pip install -q --disable-pip-version-check -r /tmp/requirements.txt \
    || echo "[spartan] WARNING: could not install $req"
done

exec /venv/bin/python server.py --host 0.0.0.0 --port 8765
