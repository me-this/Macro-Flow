"""
main.py - orchestrator. Run this on the GitHub Actions Windows RDP session.

1. Loads the hardcoded macro schedule
2. Finds events falling within this session's window (default: next 6 hours)
3. For each event, detects all open MT5 terminals and spawns one
   straddle_executor.py subprocess PER terminal
4. Waits for all subprocesses for that event to finish before moving to the next
"""
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import config
from terminal_detector import find_mt5_terminals


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


def main():
    events = get_events_in_window(hours_ahead=6)
    if not events:
        print("[main] No macro events in the next 6 hours. Nothing to do this session.")
        return

    print(f"[main] {len(events)} event(s) in this session's window:")
    for e in events:
        print(f"    {e['event']} at {e['release_time_utc'].isoformat()}")

    for e in events:
        now_utc = datetime.now(timezone.utc)
        seconds_until = (e["release_time_utc"] - now_utc).total_seconds()
        if seconds_until > 30:
            print(f"[main] Sleeping until ~20s before {e['event']}...")
            time.sleep(seconds_until - 20)
        run_event_on_all_terminals(e["event"], e["release_time_utc"])


if __name__ == "__main__":
    main()
