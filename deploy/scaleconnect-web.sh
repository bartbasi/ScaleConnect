#!/bin/sh
# Start the ScaleConnect web page if it is not running, and open it.
# Works without systemd and without sudo. Plain POSIX sh.
#
#   scaleconnect-web.sh [start] [--no-open]   start if needed, then open the page
#   scaleconnect-web.sh stop                  stop the page
#   scaleconnect-web.sh status                say if it is running
#
# The address and port come from [web] in ~/.config/scaleconnect/config.toml.
# Set SCALECONNECT_CONFIG to use another config file.

set -u
umask 077   # the state folder, log and pid file are private to this user

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/scaleconnect"
PIDFILE="$STATE/web.pid"
LOG="$STATE/web.log"
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
    # Print, and also show a pop up when there is a desktop and no terminal.
    echo "$1" >&2
    if [ -n "${DISPLAY:-}" ] && [ ! -t 2 ] && command -v notify-send >/dev/null 2>&1; then
        notify-send "Scale prices" "$1" >/dev/null 2>&1 || true
    fi
}

URL=$("$PY" -m scaleconnect.web --print-url 2>/dev/null) || {
    say "The config could not be read. Check ~/.config/scaleconnect/config.toml."
    exit 1
}

page_answers() {
    # It must be our page, not just anything that answers on the port.
    "$PY" -c "import sys, urllib.request; sys.exit(0 if b'Scale prices' in urllib.request.urlopen(sys.argv[1], timeout=3).read() else 1)" "$URL" >/dev/null 2>&1
}

sending_now() {
    "$PY" -c "import sys, urllib.request; sys.exit(0 if b'\"busy\":true' in urllib.request.urlopen(sys.argv[1] + 'api/state', timeout=3).read().replace(b' ', b'') else 1)" "$URL" >/dev/null 2>&1
}

pid_alive() {
    [ -f "$PIDFILE" ] || return 1
    PID=$(cat "$PIDFILE" 2>/dev/null) || return 1
    [ -n "$PID" ] || return 1
    kill -0 "$PID" 2>/dev/null || return 1
    # Make sure the number still belongs to our server, not to something that reused it.
    if [ -r "/proc/$PID/cmdline" ]; then
        tr '\0' ' ' < "/proc/$PID/cmdline" | grep -q "scaleconnect.web" || return 1
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
        if pid_alive || page_answers; then
            echo "Running at $URL"
            exit 0
        fi
        echo "Not running."
        exit 1
        ;;
    start) [ "$#" -gt 0 ] && shift ;;
    --no-open) ;;
    *)
        echo "Use: scaleconnect-web.sh [start] [--no-open] | stop | status" >&2
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

OPEN=yes
[ "${1:-}" = "--no-open" ] && OPEN=no

if pid_alive && page_answers; then
    :
elif page_answers; then
    :   # something of ours is answering, started another way
else
    # Keep the log small.
    if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 1000000 ]; then
        tail -n 200 "$LOG" > "$LOG.old" && mv "$LOG.old" "$LOG"
    fi
    echo "Starting at $(date)" >> "$LOG"
    # Started with no window (autostart or cron): the network may not be up yet, so try again for a while.
    if [ "$OPEN" = no ]; then MAX_TRIES=18; else MAX_TRIES=1; fi
    attempt=0
    while :; do
    attempt=$((attempt + 1))
    if command -v setsid >/dev/null 2>&1; then
        setsid nohup "$PY" -m scaleconnect.web >> "$LOG" 2>&1 < /dev/null &
    else
        nohup "$PY" -m scaleconnect.web >> "$LOG" 2>&1 < /dev/null &
    fi
    echo $! > "$PIDFILE"
    tries=0
    until page_answers; do
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
        say "The page did not start. See $LOG"
        tail -n 5 "$LOG" >&2
        exit 1
    fi
    echo "Did not start. Trying again in 5 seconds." >> "$LOG"
    sleep 5
    done
fi

echo "Running at $URL"
if [ "$OPEN" = yes ] && [ -n "${DISPLAY:-}" ]; then
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$URL" >/dev/null 2>&1 &
    else
        echo "Open $URL in a browser." >&2
    fi
fi
exit 0
