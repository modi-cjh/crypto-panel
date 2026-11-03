"""Time-limited volume test for the paper signal engine and OKX demo mirror only."""
from datetime import datetime, timezone

TEST_START = datetime(2026, 11, 3, 21, 40, tzinfo=timezone.utc)
TEST_UNTIL = datetime(2026, 11, 4, 21, 40, tzinfo=timezone.utc)
BASE_COINS = 'BTC ETH SOL XRP DOGE'.split()
TEST_COINS = BASE_COINS + 'ADA AVAX LINK LTC BCH'.split()


def profile(now=None):
    now = now or datetime.now(timezone.utc)
    active = TEST_START <= now < TEST_UNTIL
    return dict(active=active, until=TEST_UNTIL.isoformat(),
                instruments=[f'{c}-USDT-SWAP' for c in (TEST_COINS if active else BASE_COINS)],
                risk=.02 if active else .01, rsi=30 if active else 25,
                lookback=24 if active else 48)


def describe(p):
    return (f"模拟增量测试={'开启' if p['active'] else '关闭'} · 每笔风险 {p['risk']:.0%} · "
            f"扫描 {len(p['instruments'])} 币 · 到期 {p['until']}")
