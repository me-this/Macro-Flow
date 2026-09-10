"""
main.py - orchestrator. Run this on the GitHub Actions Windows RDP session.

Runs two things concurrently for the whole session:
  1. The dashboard API server (background thread) - always up, regardless
     of whether a macro event is currently scheduled.
  2. The scheduler loop (main thread) - finds the next event, logs a
     countdown starting 30 minutes out, then launches execution on every
     detected terminal at the right moment. Never exits on its own; if
     there's nothing upcoming it just idles and rechecks periodically.
"""
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import config
import api_server
from terminal_detector import find_mt5_terminals
import tunnel

def get_events_in_window(hours_ahead=6):
    now_utc = datetime.now(timezone.utc)
    window_end = now_utc + timedelta(hours=hours_ahead)
    upcoming = []
    for evt in config.MACRO_SCHEDULE_2026:
        release_utc = evt["datetime_et"].astimezone(timezone.utc)
        if now_utc < release_utc <= window_end:
            upcoming.append({"event": evt["event"], "release_time_utc": release_utc})
    upcoming.sort(key=lambda e: e["release_time_utc"])
    return upcoming


def run_event_on_all_terminals(event_name, release_time_utc):
    terminals = find_mt5_terminals()
    if not terminals:
        print(f"[main] No open MT5 terminals detected. Skipping {event_name}.")
        return

    print(f"[main] {event_name} at {release_time_utc.isoformat()} -> "
          f"launching on {len(terminals)} terminal(s): {terminals}")

    processes = []
    for terminal_path in terminals:
        cmd = [
            sys.executable, "straddle_executor.py",
            "--terminal-path", terminal_path,
            "--event-name", event_name,
            "--release-time-utc", release_time_utc.isoformat(),
        ]
        processes.append(subprocess.Popen(cmd))

    for proc in processes:
        proc.wait()

    print(f"[main] All terminals finished handling {event_name}.")


def countdown_and_launch(event_name, release_time_utc):
    """Sleeps toward the event, logging a visible countdown starting
    COUNTDOWN_LOG_START_SECONDS out, so a timezone bug shows up with
    30 minutes to fix it instead of 2 seconds."""
    while True:
        now = datetime.now(timezone.utc)
        seconds_until = (release_time_utc - now).total_seconds()

        if seconds_until <= 20:
            break

        if seconds_until <= config.COUNTDOWN_LOG_START_SECONDS:
            mins, secs = divmod(int(seconds_until), 60)
            print(f"[main] Countdown to {event_name}: T-{mins:02d}:{secs:02d} "
                  f"(release {release_time_utc.isoformat()})")
            time.sleep(min(config.COUNTDOWN_LOG_INTERVAL_SECONDS, seconds_until - 20))
        else:
            wait = seconds_until - config.COUNTDOWN_LOG_START_SECONDS
            print(f"[main] {event_name} is {wait/60:.1f} more minutes away "
                  f"before countdown logging starts.")
            time.sleep(min(wait, 300))  # wake at least every 5 min so it stays visibly alive

    run_event_on_all_terminals(event_name, release_time_utc)


def main():
    threading.Thread(target=api_server.run_api_server, daemon=True).start()
    print(f"[main] Dashboard API server running on port {config.DASHBOARD_API_PORT}.")

    time.sleep(2)  # give Flask a moment to actually bind before bore tries to relay to it
    bore_process, dashboard_url = tunnel.start_tunnel(config.DASHBOARD_API_PORT)
    print(f"[main] ============================================")
    print(f"[main] DASHBOARD READY: {dashboard_url}/")
    print(f"[main] ============================================")
    print(f"[main] Dashboard API server running on port {config.DASHBOARD_API_PORT}.")

    while True:
        events = get_events_in_window(hours_ahead=6)
        if not events:
            print("[main] No macro events in the next 6 hours. Dashboard stays up; rechecking shortly.")
            time.sleep(config.SCHEDULER_IDLE_POLL_SECONDS)
            continue

        next_event = events[0]
        countdown_and_launch(next_event["event"], next_event["release_time_utc"])


if __name__ == "__main__":
    main()
