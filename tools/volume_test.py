"""Time-limited volume test for the paper signal engine and OKX demo mirror only."""
from datetime import datetime, timezone

TEST_START = datetime(2026, 11, 3, 21, 40, tzinfo=timezone.utc)
TEST_UNTIL = datetime(2026, 11, 4, 21, 40, tzinfo=timezone.utc)
AGGRESSIVE_START = datetime(2026, 11, 3, 22, 20, tzinfo=timezone.utc)
AGGRESSIVE_UNTIL = datetime(2026, 11, 4, 22, 20, tzinfo=timezone.utc)
BASE_COINS = 'BTC ETH SOL XRP DOGE'.split()
TEST_COINS = BASE_COINS + 'ADA AVAX LINK LTC BCH'.split()
AGGRESSIVE_COINS = TEST_COINS + 'DOT NEAR SUI APT UNI'.split()


def profile(now=None):
    now = now or datetime.now(timezone.utc)
    aggressive = AGGRESSIVE_START <= now < AGGRESSIVE_UNTIL
    active = aggressive or TEST_START <= now < TEST_UNTIL
    coins = AGGRESSIVE_COINS if aggressive else TEST_COINS if active else BASE_COINS
    return dict(active=active, mode='aggressive' if aggressive else 'volume_test' if active else 'standard',
                until=(AGGRESSIVE_UNTIL if aggressive else TEST_UNTIL).isoformat(),
                instruments=[f'{c}-USDT-SWAP' for c in coins],
                scan_limit=100 if aggressive else len(coins),
                risk=.03 if aggressive else .02 if active else .01,
                same_side_cap=.06 if aggressive else .05,
                rsi=35 if aggressive else 30 if active else 25,
                trend=.03 if aggressive else .02,
                lookback=12 if aggressive else 24 if active else 48)


def describe(p):
    return (f"模拟档位={p['mode']} · 每笔风险 {p['risk']:.0%} · 同向风险上限 {p['same_side_cap']:.0%} · "
            f"扫描 {len(p['instruments'])}/{p['scan_limit']} 币 · 到期 {p['until']}")
