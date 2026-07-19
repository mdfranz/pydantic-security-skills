import time
import unittest

from skill_runner.audit import (
    RunInterrupted,
    install_sigterm_handler,
    restore_sigterm_handler,
    start_turn_watchdog,
)


class TurnWatchdogTests(unittest.TestCase):
    def test_no_budget_returns_no_timer(self):
        self.assertIsNone(start_turn_watchdog(None))

    def test_cancelled_before_it_fires_does_nothing(self):
        previous = install_sigterm_handler()
        try:
            timer = start_turn_watchdog(60)
            timer.cancel()
            time.sleep(0.05)  # long enough that a bug would have already fired
        finally:
            restore_sigterm_handler(previous)

    def test_uncancelled_watchdog_interrupts_a_stuck_turn(self):
        previous = install_sigterm_handler()
        try:
            start_turn_watchdog(0.05)
            with self.assertRaises(RunInterrupted):
                time.sleep(1)
        finally:
            restore_sigterm_handler(previous)


if __name__ == "__main__":
    unittest.main()
