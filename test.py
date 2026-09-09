"""
XAUUSD News Straddle - Order Mechanics Test Script
=====================================================
Purpose: confirm the core order-placement / management logic works BEFORE
wiring in any news calendar. This script does NOT time itself off a real
release - you trigger it manually and it immediately places the straddle,
so you can watch the behavior on a demo account.

What it does:
  1. Places N buy stop + N sell stop orders around current price
  2. Polls for fills
  3. The instant one side fills, deletes all remaining pending orders on
     the OTHER side
  4. Once a position is open, watches floating profit; when it reaches
     BREAKEVEN_TRIGGER_POINTS, moves SL to lock in BREAKEVEN_LOCK_POINTS
  5. If nothing fills within NO_FILL_TIMEOUT_SECONDS, deletes everything

Run this on a DEMO account first. Requires: pip install MetaTrader5
"""

import MetaTrader5 as mt5
import time
from datetime import datetime

# ============================================================
# CONFIG - tune these, this is the only section you should
# need to touch for testing
# ============================================================
SYMBOL = "XAUUSD"
LOT_SIZE = 0.01
NUM_ORDERS_PER_SIDE = 5

STOP_DISTANCE_POINTS = 250      # how far above/below price the stop orders sit
INITIAL_SL_POINTS = 150         # initial stop loss distance from entry
BREAKEVEN_TRIGGER_POINTS = 150  # floating profit needed to trigger breakeven move
BREAKEVEN_LOCK_POINTS = 100     # profit locked in when breakeven triggers

NO_FILL_TIMEOUT_SECONDS = 5     # delete everything if nothing fills in this window
POLL_INTERVAL_SECONDS = 0.5     # how often we check order/position state

MAGIC_NUMBER = 990001           # unique tag so we only ever touch our own orders


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}")


def connect():
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")
    info = mt5.symbol_info(SYMBOL)
    if info is None:
        raise RuntimeError(f"Symbol {SYMBOL} not found. Check it's visible in Market Watch.")
    if not info.visible:
        mt5.symbol_select(SYMBOL, True)
    log(f"Connected. Account: {mt5.account_info().login}, point size: {info.point}")
    return info


def get_filling_mode(symbol_info):
    """Pick an order filling mode the broker actually supports.
    Different brokers allow different subsets - this checks the symbol's
    filling_mode bitmask rather than assuming."""
    mode = symbol_info.filling_mode
    if mode & mt5.SYMBOL_FILLING_IOC:
        return mt5.ORDER_FILLING_IOC
    if mode & mt5.SYMBOL_FILLING_FOK:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def place_straddle(symbol_info):
    point = symbol_info.point
    tick = mt5.symbol_info_tick(SYMBOL)
    mid_price = (tick.ask + tick.bid) / 2
    filling = get_filling_mode(symbol_info)

    buy_stop_price = round(mid_price + STOP_DISTANCE_POINTS * point, symbol_info.digits)
    sell_stop_price = round(mid_price - STOP_DISTANCE_POINTS * point, symbol_info.digits)

    buy_sl = round(buy_stop_price - INITIAL_SL_POINTS * point, symbol_info.digits)
    sell_sl = round(sell_stop_price + INITIAL_SL_POINTS * point, symbol_info.digits)

    log(f"Mid price: {mid_price} | Buy stop @ {buy_stop_price} (SL {buy_sl}) | "
        f"Sell stop @ {sell_stop_price} (SL {sell_sl})")

    buy_tickets = []
    sell_tickets = []

    for i in range(NUM_ORDERS_PER_SIDE):
        buy_request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": SYMBOL,
            "volume": LOT_SIZE,
            "type": mt5.ORDER_TYPE_BUY_STOP,
            "price": buy_stop_price,
            "sl": buy_sl,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
            "magic": MAGIC_NUMBER,
            "comment": "straddle_buy",
        }
        result = mt5.order_send(buy_request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            log(f"  BUY STOP #{i+1} FAILED: retcode={result.retcode} comment={result.comment}")
        else:
            buy_tickets.append(result.order)
            log(f"  BUY STOP #{i+1} placed, ticket={result.order}")

    for i in range(NUM_ORDERS_PER_SIDE):
        sell_request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": SYMBOL,
            "volume": LOT_SIZE,
            "type": mt5.ORDER_TYPE_SELL_STOP,
            "price": sell_stop_price,
            "sl": sell_sl,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
            "magic": MAGIC_NUMBER,
            "comment": "straddle_sell",
        }
        result = mt5.order_send(sell_request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            log(f"  SELL STOP #{i+1} FAILED: retcode={result.retcode} comment={result.comment}")
        else:
            sell_tickets.append(result.order)
            log(f"  SELL STOP #{i+1} placed, ticket={result.order}")

    return buy_tickets, sell_tickets


def delete_pending_orders(tickets, label):
    for ticket in tickets:
        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": ticket,
        }
        result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            log(f"  Deleted {label} order {ticket}")
        else:
            # Order may have already filled or been removed - not fatal
            log(f"  Could not delete {label} order {ticket}: retcode={result.retcode}")


def get_still_pending(tickets):
    """Return the subset of tickets that are still open pending orders."""
    current_orders = mt5.orders_get(symbol=SYMBOL)
    current_ids = {o.ticket for o in current_orders} if current_orders else set()
    return [t for t in tickets if t in current_ids]


def get_our_positions():
    positions = mt5.positions_get(symbol=SYMBOL)
    if not positions:
        return []
    return [p for p in positions if p.magic == MAGIC_NUMBER]


def manage_breakeven(symbol_info, breakeven_done):
    point = symbol_info.point
    tick = mt5.symbol_info_tick(SYMBOL)

    for pos in get_our_positions():
        if pos.ticket in breakeven_done:
            continue

        if pos.type == mt5.ORDER_TYPE_BUY:
            profit_points = (tick.bid - pos.price_open) / point
            new_sl = round(pos.price_open + BREAKEVEN_LOCK_POINTS * point, symbol_info.digits)
        else:  # SELL position
            profit_points = (pos.price_open - tick.ask) / point
            new_sl = round(pos.price_open - BREAKEVEN_LOCK_POINTS * point, symbol_info.digits)

        if profit_points >= BREAKEVEN_TRIGGER_POINTS:
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": pos.ticket,
                "symbol": SYMBOL,
                "sl": new_sl,
                "tp": pos.tp,
            }
            result = mt5.order_send(request)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                log(f"  Breakeven SL set for position {pos.ticket}: new SL={new_sl} "
                    f"(locked {BREAKEVEN_LOCK_POINTS} pts)")
                breakeven_done.add(pos.ticket)
            else:
                log(f"  Breakeven SL modify FAILED for {pos.ticket}: "
                    f"retcode={result.retcode} comment={result.comment}")


def run():
    symbol_info = connect()
    buy_tickets, sell_tickets = place_straddle(symbol_info)

    if not buy_tickets and not sell_tickets:
        log("No orders placed successfully. Aborting.")
        return

    start_time = time.time()
    opposite_side_cleared = False
    breakeven_done = set()

    log("Monitoring for fills...")

    while True:
        elapsed = time.time() - start_time

        buy_still_pending = get_still_pending(buy_tickets)
        sell_still_pending = get_still_pending(sell_tickets)

        buy_filled = len(buy_still_pending) < len(buy_tickets)
        sell_filled = len(sell_still_pending) < len(sell_tickets)

        # As soon as one side has any fill, clear the opposite side once
        if not opposite_side_cleared and (buy_filled or sell_filled):
            if buy_filled and sell_still_pending:
                log(f"Buy side filled -> deleting {len(sell_still_pending)} unfilled sell stop(s)")
                delete_pending_orders(sell_still_pending, "sell")
            elif sell_filled and buy_still_pending:
                log(f"Sell side filled -> deleting {len(buy_still_pending)} unfilled buy stop(s)")
                delete_pending_orders(buy_still_pending, "buy")
            opposite_side_cleared = True

        # Manage breakeven on whatever positions we have
        if get_our_positions():
            manage_breakeven(symbol_info, breakeven_done)

        # Timeout: nothing filled at all -> clean up and stop
        if not opposite_side_cleared and elapsed > NO_FILL_TIMEOUT_SECONDS:
            log(f"No fill on either side after {NO_FILL_TIMEOUT_SECONDS}s. Deleting all pending orders.")
            delete_pending_orders(buy_still_pending, "buy")
            delete_pending_orders(sell_still_pending, "sell")
            break

        # If a side filled and all breakeven-eligible positions are handled,
        # keep the loop alive briefly so you can watch it in action, then
        # stop (this is a TEST script - the real bot's loop keeps running
        # until exit criteria fire, which we haven't built yet)
        if opposite_side_cleared and len(get_our_positions()) == len(breakeven_done) and \
           len(get_our_positions()) > 0 and elapsed > BREAKEVEN_TRIGGER_POINTS * 0 + 2:
            # Just keep monitoring - don't auto-exit positions in this test script
            pass

        time.sleep(POLL_INTERVAL_SECONDS)

        # Safety valve for this TEST script only - don't run forever
        if elapsed > 300:
            log("5 minute test window elapsed. Stopping monitor (positions/orders left as-is).")
            break

    log("Test run complete.")
    mt5.shutdown()


if __name__ == "__main__":
    run()
  
