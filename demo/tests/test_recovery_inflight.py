"""Characterize in-flight work during recovery; this is not a safety pass."""
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from nawa_demo.authorization import Principal
from nawa_demo.dashboard import DashboardState
from nawa_demo.protected_api import make_protected_handler
from nawa_demo.session_draft import SessionStore
from nawa_demo.synthetic_recovery_coordinator import prepare_recovery
from nawa_demo.synthetic_request_gate import SyntheticRequestGate


class RecoveryInFlightTests(unittest.TestCase):
    def test_enrolled_http_work_drains_before_synthetic_restore(self):
        entered, release, restore_called, gate_closed = (threading.Event() for _ in range(4))
        principal = Principal('synthetic-user', 'synthetic-tenant', 'analyst')
        sessions = SessionStore(lambda *_: principal)
        token = sessions.issue(principal.subject, principal.tenant)['session']
        gate = SyntheticRequestGate()

        class BlockingScoped:
            def list_items(self, actor):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test request was not released')
                return [{'id': 'synthetic'}]

        handler = make_protected_handler(BlockingScoped(), sessions, DashboardState(), synthetic_request_gate=gate)
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        url = 'http://127.0.0.1:%s/api/quotations' % server.server_port
        result, recovery = [], []
        def request():
            try:
                with urlopen(Request(url, headers={'Cookie': 'nawa_session=' + token}), timeout=5) as response:
                    result.append(response.status)
            except Exception as error:
                result.append(error)
        def close_gate():
            result = gate.close()
            gate_closed.set()
            return result
        def restore():
            restore_called.set()
            return True
        def coordinate():
            recovery.append(prepare_recovery([sessions], quiesce=close_gate, drain=lambda:gate.drain(5),
                                             restore=restore, verify=lambda:True))
        worker = threading.Thread(target=request, daemon=True)
        coordinator = threading.Thread(target=coordinate, daemon=True)
        try:
            worker.start()
            self.assertTrue(entered.wait(5))
            coordinator.start()
            self.assertTrue(gate_closed.wait(5))
            self.assertFalse(restore_called.is_set())
            with self.assertRaises(HTTPError) as rejected:
                urlopen(Request(url, headers={'Cookie': 'nawa_session=' + token}), timeout=5)
            self.assertEqual(rejected.exception.code, 503)
            rejected.exception.close()
            with self.assertRaises(HTTPError) as rejected_post:
                urlopen(Request(url, data=b'{}', headers={'Content-Type': 'application/json'}), timeout=5)
            self.assertEqual(rejected_post.exception.code, 503)
            rejected_post.exception.close()
            self.assertFalse(restore_called.is_set())
            release.set()
            worker.join(5)
            coordinator.join(5)
            self.assertFalse(worker.is_alive())
            self.assertFalse(coordinator.is_alive())
            self.assertEqual(result, [200])
            self.assertTrue(restore_called.is_set())
            self.assertEqual(recovery, [{'status': 'ready_for_manual_resume', 'completed_pause_calls': 1}])
            with self.assertRaises(HTTPError) as still_closed:
                urlopen(Request(url, headers={'Cookie': 'nawa_session=' + token}), timeout=5)
            self.assertEqual(still_closed.exception.code, 503)
            still_closed.exception.close()
            sessions.end_recovery()
            gate.resume()
            with self.assertRaises(HTTPError) as stale:
                urlopen(Request(url, headers={'Cookie': 'nawa_session=' + token}), timeout=5)
            self.assertEqual(stale.exception.code, 403)
            stale.exception.close()
            fresh = sessions.issue(principal.subject, principal.tenant)['session']
            with urlopen(Request(url, headers={'Cookie': 'nawa_session=' + fresh}), timeout=5) as response:
                self.assertEqual(response.status, 200)
        finally:
            release.set()
            worker.join(5)
            if coordinator.ident is not None:
                coordinator.join(5)
            server.shutdown()
            server.server_close()
            server_thread.join(5)

    def test_authorized_request_can_finish_after_local_pause(self):
        entered = threading.Event()
        release = threading.Event()
        principal = Principal('synthetic-user', 'synthetic-tenant', 'analyst')
        sessions = SessionStore(lambda *_: principal)
        token = sessions.issue(principal.subject, principal.tenant)['session']

        class BlockingScoped:
            def list_items(self, actor):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test request was not released')
                return [{'id': 'synthetic'}]

        server = ThreadingHTTPServer(('127.0.0.1', 0),
                                     make_protected_handler(BlockingScoped(), sessions, DashboardState()))
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        url = 'http://127.0.0.1:%s/api/quotations' % server.server_port
        result = []

        def request():
            try:
                with urlopen(Request(url, headers={'Cookie': 'nawa_session=' + token}), timeout=5) as response:
                    result.append((response.status, json.load(response)))
            except Exception as error:
                result.append(error)

        worker = threading.Thread(target=request, daemon=True)
        try:
            worker.start()
            self.assertTrue(entered.wait(5), 'synthetic request did not reach service')
            sessions.begin_recovery()
            with self.assertRaises(HTTPError) as rejected:
                urlopen(Request(url, headers={'Cookie': 'nawa_session=' + token}), timeout=5)
            self.assertEqual(rejected.exception.code, 403)
            rejected.exception.close()
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result, [(200, {'quotations': [{'id': 'synthetic'}], 'storage': 'postgres'})])
        finally:
            release.set()
            worker.join(5)
            server.shutdown()
            server.server_close()
            server_thread.join(5)


if __name__ == '__main__':
    unittest.main()
