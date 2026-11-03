import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from okx_demo_trader import decision_snapshot


class DecisionSnapshotTests(unittest.TestCase):
    def test_only_fixed_public_signal_fields_leave_the_source_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'combined').mkdir()
            rows=[dict(ts='2026-11-03T23:00:00Z',type='signal',inst='BTC-USDT-SWAP',strategy='mean_reversion',side=1,detail='PRIVATE_NOTE',chat='PRIVATE_NOTE'),
                  dict(ts='2026-11-03T23:01:00Z',type='skip',inst='BTC-USDT-SWAP',strategy='mean_reversion',reason='PRIVATE_NOTE'),
                  ['corrupt record'],dict(ts=5,type='signal'),dict(ts='2026-11-03T23:00:00',type='signal')]
            (root/'combined/events.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
            (root/'summary.json').write_text('{"updated_at":"2026-11-03T23:02:00Z"}')
            value=decision_snapshot(root)
            self.assertEqual(len(value['strategy_decisions']),2)
            self.assertEqual(value['strategy_updated_at'],'2026-11-03T23:02:00Z')
            self.assertNotIn('PRIVATE_NOTE',json.dumps(value))
            self.assertEqual(value['strategy_decisions'][0]['type'],'skip')


if __name__=='__main__':unittest.main()
