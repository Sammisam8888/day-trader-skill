"""One AI call at a time on the box; the AI trader goes first."""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402,F401
from _fake import reset  # noqa: E402

import brain  # noqa: E402
import common  # noqa: E402


class Turns(unittest.TestCase):
    def setUp(self):
        reset()

    def test_calls_never_overlap(self):
        live, worst = [0], [0]

        def call():
            with brain.cli_turn(wait=10):
                live[0] += 1
                worst[0] = max(worst[0], live[0])
                time.sleep(0.2)
                live[0] -= 1
        ts = [threading.Thread(target=call) for _ in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(worst[0], 1)

    def test_a_background_job_holds_back_while_the_trader_waits_and_gives_up_in_time(self):
        (common.STATE_DIR / "cli").mkdir(parents=True, exist_ok=True)
        (common.STATE_DIR / "cli" / "trader.want").touch()
        t0 = time.monotonic()
        with self.assertRaises(brain.CliBusy):
            with brain.cli_turn(low=True, wait=1):
                pass
        self.assertLess(time.monotonic() - t0, 3)
        with brain.cli_turn(wait=1):                    # the trader itself is not blocked by its own flag
            pass
        self.assertFalse((common.STATE_DIR / "cli" / "trader.want").exists())

    def test_a_stale_trader_flag_does_not_block_forever(self):
        (common.STATE_DIR / "cli").mkdir(parents=True, exist_ok=True)
        f = common.STATE_DIR / "cli" / "trader.want"
        f.touch()
        os.utime(f, (time.time() - 600, time.time() - 600))
        with brain.cli_turn(low=True, wait=1):
            pass


if __name__ == "__main__":
    unittest.main()
