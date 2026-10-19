"""
market_order_latency_test.py - standalone test for a NEW market-order-based
algo (opens real positions directly, no stop orders). This measures what we
need before writing config for it: round-trip order_send latency, requested
vs actual fill price (slippage on a MARKET order, not a stop order), filling
mode the broker accepts, and basic close-latency too.

Sequence: BUY (200pt SL, 300pt TP) -> wait 5s -> close -> SELL (same) -> wait 5s -> close.

Run directly: python market_order_latency_test.py
"""

# ---- dependency check / auto-install ----
import subprocess
import sys

try:
    import MetaTrader5 as mt5
except ImportError:
    print("MetaTrader5 package not found - installing...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "MetaTrader5"])
    import MetaTrader5 as mt5

import time
from datetime import datetime, timezone

SYMBOL = "XAUUSD"
SL_POINTS = 200
TP_POINTS = 300
HOLD_SECONDS = 5


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}")


def get_filling_mode(symbol_info):
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2
    mode = symbol_info.filling_mode
    if mode & SYMBOL_FILLING_IOC:
        return "IOC", mt5.ORDER_FILLING_IOC
    if mode & SYMBOL_FILLING_FOK:
        return "FOK", mt5.ORDER_FILLING_FOK
    return "RETURN", mt5.ORDER_FILLING_RETURN


def connect():
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")
    info = mt5.symbol_info(SYMBOL)
    if info is None:
        raise RuntimeError(f"Symbol {SYMBOL} not found - check it's visible in Market Watch.")
    if not info.visible:
        mt5.symbol_select(SYMBOL, True)
        info = mt5.symbol_info(SYMBOL)

    acct = mt5.account_info()
    log(f"CONNECTED. Account={acct.login} Broker={acct.company} "
        f"Balance={acct.balance} Equity={acct.equity}")
    log(f"SYMBOL INFO: point={info.point} digits={info.digits} "
        f"stops_level={info.trade_stops_level} freeze_level={info.trade_freeze_level}")

    mode_name, mode_const = get_filling_mode(info)
    log(f"FILLING MODE: broker reports support for '{mode_name}' (raw bitmask={info.filling_mode})")

    return info, mode_const


def open_market_order(symbol_info, filling_mode, side):
    """side: 'buy' or 'sell'. Returns the position ticket."""
    point = symbol_info.point
    tick = mt5.symbol_info_tick(SYMBOL)

    if side == "buy":
        order_type = mt5.ORDER_TYPE_BUY
        requested_price = tick.ask
        sl = round(requested_price - SL_POINTS * point, symbol_info.digits)
        tp = round(requested_price + TP_POINTS * point, symbol_info.digits)
    else:
        order_type = mt5.ORDER_TYPE_SELL
        requested_price = tick.bid
        sl = round(requested_price + SL_POINTS * point, symbol_info.digits)
        tp = round(requested_price - TP_POINTS * point, symbol_info.digits)

    log(f"[{side.upper()}] Requesting market order. requested_price={requested_price} sl={sl} tp={tp}")

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": SYMBOL,
        "volume": 0.01,
        "type": order_type,
        "price": requested_price,
        "sl": sl,
        "tp": tp,
        "type_filling": filling_mode,
        "magic": 777001,
        "comment": f"market_test_{side}",
    }

    t_before = time.perf_counter()
    result = mt5.order_send(req)
    t_after = time.perf_counter()
    latency_ms = (t_after - t_before) * 1000

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"[{side.upper()}] ORDER FAILED retcode={result.retcode} comment={result.comment} "
            f"(round-trip latency={latency_ms:.1f}ms)")
        return None

    # Pull the actual position to see real fill price vs what we requested
    time.sleep(0.1)  # tiny buffer for position to register locally
    positions = mt5.positions_get(ticket=result.order)
    actual_price = positions[0].price_open if positions else result.price

    slippage_points = (actual_price - requested_price) / point if side == "buy" else (requested_price - actual_price) / point

    log(f"[{side.upper()}] ORDER DONE. ticket={result.order} "
        f"requested={requested_price} actual_fill={actual_price} "
        f"slippage={slippage_points:+.1f}pts round_trip_latency={latency_ms:.1f}ms "
        f"deal_retcode={result.retcode}")

    return result.order


def close_position(symbol_info, filling_mode, ticket, side):
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        log(f"[{side.upper()}] CLOSE SKIPPED - position {ticket} not found (already closed? SL/TP hit?)")
        return

    pos = positions[0]
    tick = mt5.symbol_info_tick(SYMBOL)

    if side == "buy":
        close_type = mt5.ORDER_TYPE_SELL
        close_price = tick.bid
    else:
        close_type = mt5.ORDER_TYPE_BUY
        close_price = tick.ask

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": SYMBOL,
        "volume": pos.volume,
        "type": close_type,
        "position": ticket,
        "price": close_price,
        "type_filling": filling_mode,
        "magic": 777001,
        "comment": f"market_test_close_{side}",
    }

    t_before = time.perf_counter()
    result = mt5.order_send(req)
    t_after = time.perf_counter()
    latency_ms = (t_after - t_before) * 1000

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"[{side.upper()}] CLOSE FAILED retcode={result.retcode} comment={result.comment} "
            f"(round-trip latency={latency_ms:.1f}ms)")
        return

    point = symbol_info.point
    pnl_points = (close_price - pos.price_open) if side == "buy" else (pos.price_open - close_price)
    pnl_points /= point

    log(f"[{side.upper()}] CLOSED. ticket={ticket} entry={pos.price_open} exit={close_price} "
        f"result={pnl_points:+.1f}pts round_trip_latency={latency_ms:.1f}ms")


def run():
    symbol_info, filling_mode = connect()

    log("=" * 60)
    log("TEST 1: BUY market order")
    log("=" * 60)
    buy_ticket = open_market_order(symbol_info, filling_mode, "buy")
    if buy_ticket:
        log(f"Holding for {HOLD_SECONDS}s...")
        time.sleep(HOLD_SECONDS)
        close_position(symbol_info, filling_mode, buy_ticket, "buy")

    log("=" * 60)
    log("TEST 2: SELL market order")
    log("=" * 60)
    sell_ticket = open_market_order(symbol_info, filling_mode, "sell")
    if sell_ticket:
        log(f"Holding for {HOLD_SECONDS}s...")
        time.sleep(HOLD_SECONDS)
        close_position(symbol_info, filling_mode, sell_ticket, "sell")

    log("TEST COMPLETE.")
    mt5.shutdown()


if __name__ == "__main__":
    run()
