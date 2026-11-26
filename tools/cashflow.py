"""External cash flows of the trading account (deposits, withdrawals, fund transfers) for the daily halt.

CASHFLOW HALT RULE (2026-11-26): the live / demo follower's daily halt (`okx_demo_trader.DAILY_HALT`, 25 %) compares
today's equity with `day_start`. Money moved in or out of the trading account is not a trading result, so it is taken
out first:

    adjusted_day_pnl = (equity - day_start) - net_flow

net_flow = signed sum of `balChg` of the USDT transfer bills of the TRADING account in the window
[max(00:00 UTC+7 of `state['day']`, `state['day_start_ts']`), the run's clock (`Mirror.now_ms`)].

Source: GET /api/v5/account/bills (trading account, last 7 days, read-only, limit 100, `after` = bill-id cursor).
On OKX a deposit lands in the Funding account and a withdrawal is paid from it; what moves the trading-account balance
is the transfer funding <-> trading (also sub-account transfers), which the trading account books as bill type 1
"Transfer" (subType 11 "Transfer in" / 12 "Transfer out", ...). A web withdrawal that draws on the trading balance
books the same transfer-out first. So type 1 of account/bills is the one source; asset/bills, deposit-history and
withdrawal-history describe the Funding account and are NOT added (that would count the same money twice).

Only USDT is counted: the follower's equity is the USDT `eq` of /account/balance?ccy=USDT, so a transfer of another
currency (USDC, BTC ...) does not move it at all and must not be subtracted.

Only GET requests are sent from here (no transfer, no withdrawal, no POST). Any failure raises CashflowError with a
short reason; the caller falls back to the last good value (okx_demo_trader.Mirror.day_cashflow).
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

UTC7 = timezone(timedelta(hours=7))                # same day convention as okx_demo_trader.local_day
CCY = 'USDT'
CASHFLOW_TYPES = frozenset({'1'})                  # account/bills type 1 = Transfer (in / out, funding, sub-account)
PAGE_LIMIT = 100
MAX_PAGES = 10                                     # 1 000 transfer bills in one day = something is wrong: give up
BILLS_PATH = '/api/v5/account/bills'


class CashflowError(Exception):
    """Bills could not be read or trusted; `reason` is short and never holds credentials."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def day_start_ms(day):
    """00:00 UTC+7 of an ISO date ('2026-11-26') in ms; None when the day is missing or malformed."""
    try:
        d = datetime.fromisoformat(str(day)).date()
    except (TypeError, ValueError):
        return None
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC7).timestamp() * 1000)


def window_start(state, now_ms):
    """Start of the cash-flow window: flows before `day_start` was captured are already inside it. A state without
    `day_start_ts` (written before this rule) falls back to the start of its day."""
    begin = day_start_ms(state.get('day'))
    try:
        stamped = int(state.get('day_start_ts') or 0)
    except (TypeError, ValueError):
        stamped = 0
    begin = max(begin or 0, stamped)
    return min(begin, now_ms) if begin else now_ms


def fetch_bills(request, begin_ms, end_ms, max_pages=MAX_PAGES):
    """USDT transfer bills with begin_ms <= ts <= end_ms, newest first, de-duplicated by billId.
    `request(method, path)` is the signed OKX client (GET only)."""
    base = f'{BILLS_PATH}?ccy={CCY}&type=1&begin={int(begin_ms)}&end={int(end_ms)}&limit={PAGE_LIMIT}'
    seen, bills, cursor = set(), [], None
    for _ in range(max_pages):
        try:
            r = request('GET', base + (f'&after={cursor}' if cursor else ''))
        except Exception as e:                         # noqa: BLE001  network / client error: never fatal here
            raise CashflowError(f'request_failed:{type(e).__name__}') from None
        if not isinstance(r, dict):
            raise CashflowError('malformed_response')
        if str(r.get('code')) != '0':
            raise CashflowError(f"code_{str(r.get('code'))[:12]}")
        data = r.get('data')
        if not isinstance(data, list) or any(not isinstance(b, dict) or not b.get('billId') for b in data):
            raise CashflowError('malformed_data')
        oldest = None
        for b in data:
            ts = _int(b.get('ts'))
            oldest = ts if oldest is None else min(oldest, ts)
            if b['billId'] in seen:
                continue
            seen.add(b['billId'])
            bills.append(b)
        if len(data) < PAGE_LIMIT or oldest < begin_ms:
            return bills
        nxt = str(data[-1]['billId'])
        if nxt == cursor:
            raise CashflowError('pagination_stuck')
        cursor = nxt
    raise CashflowError('page_cap')


def net_flow(bills, begin_ms, end_ms):
    """(net USDT flow, count) of the transfer bills inside the window; positive = money in."""
    total, count = Decimal(0), 0
    for b in bills:
        if str(b.get('type')) not in CASHFLOW_TYPES or b.get('ccy') != CCY:
            continue
        ts = _int(b.get('ts'))
        if not begin_ms <= ts <= end_ms:
            continue
        try:
            chg = Decimal(str(b.get('balChg')))
        except (InvalidOperation, ValueError):
            raise CashflowError('malformed_balchg') from None
        if not chg.is_finite():
            raise CashflowError('malformed_balchg')
        total += chg; count += 1
    return float(total), count


def compute(request, begin_ms, end_ms, max_pages=MAX_PAGES):
    return net_flow(fetch_bills(request, begin_ms, end_ms, max_pages), begin_ms, end_ms)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise CashflowError('malformed_ts') from None
