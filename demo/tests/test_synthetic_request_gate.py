"""Synthetic single-process request gate; no production traffic is controlled."""
import threading
import unittest

from nawa_demo.synthetic_recovery_coordinator import prepare_recovery
from nawa_demo.synthetic_request_gate import GateClosed, SyntheticRequestGate


class SyntheticRequestGateTests(unittest.TestCase):
    def test_drain_waits_for_enrolled_work_and_blocks_new_work(self):
        gate = SyntheticRequestGate()
        entered, release, draining = (threading.Event() for _ in range(3))
        result = []
        def work():
            with gate.operation():
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test release timed out')
        worker = threading.Thread(target=work)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertTrue(gate.close())
            with self.assertRaises(GateClosed):
                with gate.operation():
                    self.fail('new work entered a closed gate')
            def drain():
                draining.set()
                result.append(gate.drain(5))
            waiter = threading.Thread(target=drain)
            waiter.start()
            self.assertTrue(draining.wait(5))
            self.assertFalse(gate.drain(0))
            with self.assertRaises(GateClosed):
                gate.resume()
            release.set()
            worker.join(5)
            waiter.join(5)
            self.assertFalse(worker.is_alive())
            self.assertFalse(waiter.is_alive())
            self.assertEqual(result, [True])
            gate.resume()
            with gate.operation():
                pass
        finally:
            release.set()
            worker.join(5)

    def test_timeout_keeps_gate_closed_and_prevents_restore(self):
        gate = SyntheticRequestGate()
        calls = []
        class Store:
            def begin_recovery(self):
                calls.append('pause')
        with gate.operation():
            result = prepare_recovery([Store()], quiesce=gate.close, drain=lambda:gate.drain(0),
                                      restore=lambda:calls.append('restore') or True, verify=lambda:True)
            self.assertEqual(result['stage'], 'drain')
            self.assertEqual(calls, [])
            with self.assertRaises(GateClosed):
                with gate.operation():
                    pass
        self.assertTrue(gate.drain(0))
        gate.resume()

    def test_exceptions_release_enrolled_work(self):
        gate = SyntheticRequestGate()
        with self.assertRaisesRegex(RuntimeError, 'synthetic'):
            with gate.operation():
                raise RuntimeError('synthetic')
        self.assertTrue(gate.close())
        self.assertTrue(gate.drain(0))
        gate.resume()

    def test_invalid_drain_and_double_close_fail_safely(self):
        gate = SyntheticRequestGate()
        self.assertFalse(gate.drain(0))
        with self.assertRaises(ValueError):
            gate.drain(float('nan'))
        self.assertTrue(gate.close())
        self.assertFalse(gate.close())
        gate.resume()


if __name__ == '__main__':
    unittest.main()
