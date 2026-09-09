"""
config.py - central configuration: trading parameters + hardcoded 2026 macro schedule
"""
from datetime import datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

# ============================================================
# TRADING PARAMETERS
# ============================================================
SYMBOL = "XAUUSD"
LOT_SIZE = 0.01
NUM_ORDERS_PER_SIDE = 5
EXIT_SECONDS_AFTER_RELEASE = 60   # close ALL positions exactly this many seconds after release, no exceptions
STOP_DISTANCE_POINTS = 250
INITIAL_SL_POINTS = 150
BREAKEVEN_TRIGGER_POINTS = 150
BREAKEVEN_LOCK_POINTS = 100
DASHBOARD_API_PORT = 8787
EXECUTION_LOG_PATH = "execution_log.jsonl"
COUNTDOWN_LOG_START_SECONDS = 30 * 60   # start logging countdown 30 min before release
COUNTDOWN_LOG_INTERVAL_SECONDS = 60     # log remaining time this often during the countdown
SCHEDULER_IDLE_POLL_SECONDS = 60        # how often to recheck when no event is upcoming
PRE_RELEASE_LEAD_SECONDS = 2                # place orders this many seconds BEFORE release
POST_RELEASE_NO_FILL_TIMEOUT_SECONDS = 7    # delete unfilled orders this many seconds AFTER release
POLL_INTERVAL_SECONDS = 0.25

MAGIC_NUMBER = 990001

# ============================================================
# HARDCODED MACRO SCHEDULE - remaining 2026 events
# Source: bls.gov/schedule/news_release/ + federalreserve.gov/monetarypolicy/fomccalendars.htm
# Times are ET wall-clock; zoneinfo resolves EDT/EST automatically.
# ============================================================
MACRO_SCHEDULE_2026 = [
    {"event": "Non-Farm Employment Change", "datetime_et": datetime(2026, 10, 2, 8, 30, tzinfo=EASTERN)},
    {"event": "Non-Farm Employment Change", "datetime_et": datetime(2026, 11, 6, 8, 30, tzinfo=EASTERN)},
    {"event": "Non-Farm Employment Change", "datetime_et": datetime(2026, 12, 4, 8, 30, tzinfo=EASTERN)},

    {"event": "CPI",                        "datetime_et": datetime(2026, 9, 11, 8, 30, tzinfo=EASTERN)},
    {"event": "CPI",                        "datetime_et": datetime(2026, 10, 14, 8, 30, tzinfo=EASTERN)},
    {"event": "CPI",                        "datetime_et": datetime(2026, 11, 10, 8, 30, tzinfo=EASTERN)},
    {"event": "CPI",                        "datetime_et": datetime(2026, 12, 10, 8, 30, tzinfo=EASTERN)},

    {"event": "PPI",                        "datetime_et": datetime(2026, 9, 10, 8, 30, tzinfo=EASTERN)},
    {"event": "PPI",                        "datetime_et": datetime(2026, 10, 15, 8, 30, tzinfo=EASTERN)},
    {"event": "PPI",                        "datetime_et": datetime(2026, 11, 13, 8, 30, tzinfo=EASTERN)},
    {"event": "PPI",                        "datetime_et": datetime(2026, 12, 15, 8, 30, tzinfo=EASTERN)},

    {"event": "FOMC Statement",             "datetime_et": datetime(2026, 9, 16, 14, 0, tzinfo=EASTERN)},
    {"event": "FOMC Statement",             "datetime_et": datetime(2026, 10, 28, 14, 0, tzinfo=EASTERN)},
    {"event": "FOMC Statement",             "datetime_et": datetime(2026, 12, 9, 14, 0, tzinfo=EASTERN)},
]
