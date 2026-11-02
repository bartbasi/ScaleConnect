#!/bin/sh
# Start the ScaleConnect MCP server (HTTP) if it is not running.
# Works without systemd and without sudo. Plain POSIX sh.
#
#   scaleconnect-mcp.sh [start]     start if needed
#   scaleconnect-mcp.sh stop        stop it (refuses during a send, unless: stop --force)
#   scaleconnect-mcp.sh status      say if it is running
#
# The address and port come from [mcp] in ~/.config/scaleconnect/config.toml.
# Set SCALECONNECT_CONFIG to use another config file.
# The server needs [auth] mcp_token in the config. Without it, it will not start.

set -u
umask 077   # the state folder, log and pid file are private to this user

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/scaleconnect"
PIDFILE="$STATE/mcp.pid"
LOG="$STATE/mcp.log"
mkdir -p "$STATE"

if [ -x "$REPO/.venv/bin/python" ]; then
    PY="$REPO/.venv/bin/python"
else
    PY=$(command -v python3 || command -v python || true)
fi
if [ -z "$PY" ]; then
    echo "Python was not found. Install Python 3 or make $REPO/.venv first." >&2
    exit 1
fi
PYTHONPATH="$REPO/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

say() {
    echo "$1" >&2
    if [ -n "${DISPLAY:-}" ] && [ ! -t 2 ] && command -v notify-send >/dev/null 2>&1; then
        notify-send "Scale MCP server" "$1" >/dev/null 2>&1 || true
    fi
}

# The address the server listens on, from the config: host:port
ADDR=$("$PY" -c "
import sys
from scaleconnect.config import load_config
c = load_config()
print('%s:%s' % (c.mcp.host, c.mcp.port))" 2>/dev/null) || {
    say "The config could not be read. Check ~/.config/scaleconnect/config.toml."
    exit 1
}
URL="http://$ADDR/mcp"

server_answers() {
    # With no token the server must say 401. That proves it is up, and that it is ours.
    "$PY" -c "
import sys, urllib.error, urllib.request
try:
    urllib.request.urlopen(sys.argv[1], timeout=3)
except urllib.error.HTTPError as e:
    sys.exit(0 if e.code == 401 else 1)
except Exception:
    sys.exit(1)
sys.exit(1)" "$URL" >/dev/null 2>&1
}

sending_now() {
    # A send holds the apply lock. If we cannot take it, a send is running.
    "$PY" -c "
import sys
from scaleconnect.config import load_config
from scaleconnect.errors import ApplyBusy
from scaleconnect.service import _apply_lock
c = load_config()
try:
    with _apply_lock(c.settings.state_dir / 'apply.lock'):
        pass
except ApplyBusy:
    sys.exit(0)
sys.exit(1)" >/dev/null 2>&1
}

pid_alive() {
    [ -f "$PIDFILE" ] || return 1
    PID=$(cat "$PIDFILE" 2>/dev/null) || return 1
    [ -n "$PID" ] || return 1
    kill -0 "$PID" 2>/dev/null || return 1
    # Make sure the number still belongs to our server, not to something that reused it.
    if [ -r "/proc/$PID/cmdline" ]; then
        tr '\0' ' ' < "/proc/$PID/cmdline" | grep -q "scaleconnect.mcp_server" || return 1
    fi
    return 0
}

case "${1:-start}" in
    stop)
        if sending_now && [ "${2:-}" != "--force" ]; then
            echo "A change is being sent to the scales right now. Wait for it to finish." >&2
            echo "To stop anyway, use: stop --force. The change will then need to be closed by hand." >&2
            exit 1
        fi
        if pid_alive; then
            kill "$PID" && echo "Stopped."
            rm -f "$PIDFILE"
        else
            echo "It is not running."
            rm -f "$PIDFILE"
        fi
        exit 0
        ;;
    status)
        if pid_alive || server_answers; then
            echo "Running at $URL"
            exit 0
        fi
        echo "Not running."
        exit 1
        ;;
    start) ;;
    *)
        echo "Use: scaleconnect-mcp.sh [start] | stop [--force] | status" >&2
        exit 2
        ;;
esac

# The config holds the PIN and the token, so only this user may read it.
CONF="${SCALECONNECT_CONFIG:-$HOME/.config/scaleconnect/config.toml}"
if [ -f "$CONF" ]; then
    MODE=$(stat -c %a "$CONF" 2>/dev/null || echo 600)
    case "$MODE" in
        600|400) ;;
        *)
            say "The config file can be read by other users. Run: chmod 600 $CONF"
            exit 1
            ;;
    esac
fi

if pid_alive && server_answers; then
    :
elif server_answers; then
    :   # something of ours is answering, started another way
else
    # Keep the log small.
    if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 1000000 ]; then
        tail -n 200 "$LOG" > "$LOG.old" && mv "$LOG.old" "$LOG"
    fi
    echo "Starting at $(date)" >> "$LOG"
    # The network may not be up yet at login or boot, so try again for a while.
    MAX_TRIES=18
    attempt=0
    while :; do
        attempt=$((attempt + 1))
        if command -v setsid >/dev/null 2>&1; then
            setsid nohup "$PY" -m scaleconnect.mcp_server --http >> "$LOG" 2>&1 < /dev/null &
        else
            nohup "$PY" -m scaleconnect.mcp_server --http >> "$LOG" 2>&1 < /dev/null &
        fi
        echo $! > "$PIDFILE"
        tries=0
        until server_answers; do
            tries=$((tries + 1))
            if [ "$tries" -ge 20 ] || ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
                tries=-1
                break
            fi
            sleep 1
        done
        [ "$tries" -ge 0 ] && break
        rm -f "$PIDFILE"
        if [ "$attempt" -ge "$MAX_TRIES" ]; then
            say "The MCP server did not start. See $LOG"
            tail -n 5 "$LOG" >&2
            exit 1
        fi
        echo "Did not start. Trying again in 5 seconds." >> "$LOG"
        sleep 5
    done
fi

echo "Running at $URL"
exit 0
