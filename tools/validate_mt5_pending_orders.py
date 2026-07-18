"""Regression test for the new pending-order methods added to
src/infrastructure/mt5_bridge/mt5_gateway.py (place_pending_order,
poll_pending_orders, cancel_pending_order) -- strategy_v2 Module 7 (MT5
Execution). Neither `MetaTrader5` nor `mt5linux` is importable on this
host (this code normally runs inside the Wine/MT5 container) -- so, same
convention as tools/validate_ws2_signal_dedup.py, this test drives the
REAL bound methods against a stubbed `self` and a fake `mt5` module
object monkey-patched into the gateway module's namespace. This tests
request-building and response-handling/retry logic thoroughly without
ever needing a live connection.

HOW TO RUN
    python3 tools/validate_mt5_pending_orders.py   (from repo root; no env vars needed)
"""
import sys, types
sys.path.insert(0, ".")
import src.infrastructure.mt5_bridge.mt5_gateway as gw_module
from src.infrastructure.mt5_bridge.mt5_gateway import MT5Gateway

FAILS = []
def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({extra})" if extra else ""))
    if not cond:
        FAILS.append(label)


class FakeResult:
    def __init__(self, retcode, order=999, price=1.1000, volume=0.10):
        self.retcode, self.order, self.price, self.volume = retcode, order, price, volume


class FakeOrder:
    def __init__(self, ticket, symbol, otype, price_open, sl, tp, volume_current, time_setup, time_expiration, magic):
        self.ticket, self.symbol, self.type = ticket, symbol, otype
        self.price_open, self.sl, self.tp = price_open, sl, tp
        self.volume_current, self.time_setup, self.time_expiration, self.magic = volume_current, time_setup, time_expiration, magic


class FakeMT5:
    TRADE_ACTION_PENDING = "PENDING"
    TRADE_ACTION_REMOVE = "REMOVE"
    ORDER_TYPE_BUY_LIMIT = "BUY_LIMIT"
    ORDER_TYPE_SELL_LIMIT = "SELL_LIMIT"
    ORDER_TIME_SPECIFIED = "SPECIFIED"
    ORDER_FILLING_IOC = "IOC"
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_REQUOTE = 10004
    TRADE_RETCODE_PRICE_CHANGED = 10020
    TRADE_RETCODE_TIMEOUT = 10008
    TRADE_RETCODE_CONNECTION = 10031
    TRADE_RETCODE_PRICE_OFF = 10021
    TRADE_RETCODE_NO_MONEY = 10019  # permanent, NOT in the retryable set

    def __init__(self):
        self.send_call_count = 0
        self.responses = []  # queue of FakeResult (or None) to return in order
        self.last_request = None
        self.orders_to_return = []

    def order_send(self, request):
        self.send_call_count += 1
        self.last_request = request
        if not self.responses:
            return FakeResult(self.TRADE_RETCODE_DONE)
        return self.responses.pop(0)

    def orders_get(self, symbol=None):
        return self.orders_to_return


def mk_stub(fake):
    gw_module.mt5 = fake
    stub = types.SimpleNamespace(_ensure_ready=lambda: True)
    stub._retryable_retcode = lambda retcode: MT5Gateway._retryable_retcode(stub, retcode)
    return stub


BASE_ORDER = dict(symbol="EURUSD", type="BUY", volume=0.10, price=1.1000, sl=1.0980, tp=1.1100, expiration=1800000000)

print("="*78); print("SECTION 1 — SUCCESSFUL PLACEMENT"); print("="*78)

fake = FakeMT5()
stub = mk_stub(fake)
result = MT5Gateway.place_pending_order(stub, BASE_ORDER, retries=3, retry_delay=0)
check("1a successful placement returns a result dict", result is not None and result["ticket"] == 999)
check("1b exactly one order_send call on immediate success", fake.send_call_count == 1)
check("1c request uses TRADE_ACTION_PENDING", fake.last_request["action"] == FakeMT5.TRADE_ACTION_PENDING)
check("1d BUY order maps to ORDER_TYPE_BUY_LIMIT", fake.last_request["type"] == FakeMT5.ORDER_TYPE_BUY_LIMIT)
check("1e expiration passed through correctly", fake.last_request["expiration"] == 1800000000)
check("1f type_time is ORDER_TIME_SPECIFIED", fake.last_request["type_time"] == FakeMT5.ORDER_TIME_SPECIFIED)

fake_sell = FakeMT5()
stub_sell = mk_stub(fake_sell)
MT5Gateway.place_pending_order(stub_sell, {**BASE_ORDER, "type": "SELL"}, retries=3, retry_delay=0)
check("1g SELL order maps to ORDER_TYPE_SELL_LIMIT", fake_sell.last_request["type"] == FakeMT5.ORDER_TYPE_SELL_LIMIT)

print(); print("="*78); print("SECTION 2 — RETRY LOGIC: transient failure then success"); print("="*78)

fake2 = FakeMT5()
fake2.responses = [FakeResult(FakeMT5.TRADE_RETCODE_REQUOTE), FakeResult(FakeMT5.TRADE_RETCODE_PRICE_CHANGED), FakeResult(FakeMT5.TRADE_RETCODE_DONE)]
stub2 = mk_stub(fake2)
result2 = MT5Gateway.place_pending_order(stub2, BASE_ORDER, retries=3, retry_delay=0)
check("2a succeeds after 2 retryable failures", result2 is not None)
check("2b order_send called exactly 3 times (2 retries + success)", fake2.send_call_count == 3)

print(); print("="*78); print("SECTION 3 — RETRY EXHAUSTION"); print("="*78)

fake3 = FakeMT5()
fake3.responses = [FakeResult(FakeMT5.TRADE_RETCODE_TIMEOUT)] * 5
stub3 = mk_stub(fake3)
result3 = MT5Gateway.place_pending_order(stub3, BASE_ORDER, retries=3, retry_delay=0)
check("3a returns None after exhausting all retries", result3 is None)
check("3b order_send called exactly `retries` times (3), not more", fake3.send_call_count == 3)

print(); print("="*78); print("SECTION 4 — NON-RETRYABLE FAILURE (no wasted retries)"); print("="*78)

fake4 = FakeMT5()
fake4.responses = [FakeResult(FakeMT5.TRADE_RETCODE_NO_MONEY)] * 3
stub4 = mk_stub(fake4)
result4 = MT5Gateway.place_pending_order(stub4, BASE_ORDER, retries=3, retry_delay=0)
check("4a returns None immediately on a permanent rejection", result4 is None)
check("4b order_send called only ONCE (no retry wasted on a non-retryable code)", fake4.send_call_count == 1)

print(); print("="*78); print("SECTION 5 — order_send RETURNS None (severed connection)"); print("="*78)

fake5 = FakeMT5()
fake5.responses = [None]
stub5 = mk_stub(fake5)
result5 = MT5Gateway.place_pending_order(stub5, BASE_ORDER, retries=3, retry_delay=0)
check("5a None result treated as terminal (not retryable), returns None", result5 is None)
check("5b only one attempt made", fake5.send_call_count == 1)

print(); print("="*78); print("SECTION 6 — poll_pending_orders"); print("="*78)

fake6 = FakeMT5()
fake6.orders_to_return = [
    FakeOrder(101, "EURUSD", FakeMT5.ORDER_TYPE_BUY_LIMIT, 1.1000, 1.0980, 1.1100, 0.10, 1000, 2000, 202400),
    FakeOrder(102, "GBPUSD", FakeMT5.ORDER_TYPE_SELL_LIMIT, 1.3000, 1.3020, 1.2900, 0.20, 1001, 2001, 999999),
]
stub6 = mk_stub(fake6)
orders = MT5Gateway.poll_pending_orders(stub6)
check("6a returns all pending orders when unfiltered", len(orders) == 2)
check("6b BUY_LIMIT correctly mapped to 'BUY'", orders[0]["type"] == "BUY")
check("6c SELL_LIMIT correctly mapped to 'SELL'", orders[1]["type"] == "SELL")
check("6d fields correctly extracted", orders[0]["ticket"] == 101 and orders[0]["price_open"] == 1.1000)

filtered = MT5Gateway.poll_pending_orders(stub6, magic=202400)
check("6e magic filter narrows to matching orders only", len(filtered) == 1 and filtered[0]["ticket"] == 101)

fake6_empty = FakeMT5()
fake6_empty.orders_to_return = []
stub6_empty = mk_stub(fake6_empty)
check("6f no pending orders: returns empty list, not None", MT5Gateway.poll_pending_orders(stub6_empty) == [])

print(); print("="*78); print("SECTION 7 — cancel_pending_order"); print("="*78)

fake7 = FakeMT5()
stub7 = mk_stub(fake7)
cancel_result = MT5Gateway.cancel_pending_order(stub7, 101)
check("7a successful cancellation returns a result dict", cancel_result is not None and cancel_result["ticket"] == 101)
check("7b request uses TRADE_ACTION_REMOVE", fake7.last_request["action"] == FakeMT5.TRADE_ACTION_REMOVE)
check("7c request references the correct order ticket", fake7.last_request["order"] == 101)

fake7_fail = FakeMT5()
fake7_fail.responses = [FakeResult(FakeMT5.TRADE_RETCODE_NO_MONEY)]  # any non-DONE code
stub7_fail = mk_stub(fake7_fail)
check("7d failed cancellation returns None", MT5Gateway.cancel_pending_order(stub7_fail, 101) is None)

print(); print("="*78)
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
else:
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)
