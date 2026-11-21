"""Phase 10 container entrypoint tests; Docker daemon not needed."""
import contextlib
import io
import os
import unittest
from unittest.mock import MagicMock, patch

from nawa_demo import dashboard_container


class DockerEntrypointTests(unittest.TestCase):
    def test_synthetic_evidence_requires_disposable_ci_stack(self):
        with patch.dict(os.environ, {'NAWA_SYNTHETIC_EVIDENCE':'ci'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'disposable CI'):
                dashboard_container.serve()
        with patch.dict(os.environ, {'NAWA_SYNTHETIC_EVIDENCE':'unexpected'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'Invalid synthetic evidence'):
                dashboard_container.serve()
    @patch('nawa_demo.dashboard_container.HTTPServer')
    def test_configurable_test_port_and_invalid_port(self, http_cls):
        with patch.dict(os.environ, {'NAWA_BIND_PORT':'8766'}, clear=True):
            with contextlib.redirect_stdout(io.StringIO()):
                dashboard_container.serve()
        self.assertEqual(http_cls.call_args.args[0], ('0.0.0.0', 8766))
        with patch.dict(os.environ, {'NAWA_BIND_PORT':'80'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'unprivileged'):
                dashboard_container.serve()

    def test_intake_cannot_be_enabled_in_demo_mode(self):
        with patch.dict(os.environ, {'NAWA_INTAKE': 'synthetic', 'NAWA_AUTH': 'demo', 'NAWA_STORAGE': 'memory'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'requires OIDC and PostgreSQL'):
                dashboard_container.serve()

    def test_invalid_synthetic_policy_fails_before_database_connection(self):
        settings = {'NAWA_INTAKE':'synthetic', 'NAWA_AUTH':'oidc', 'NAWA_STORAGE':'postgres',
                    'NAWA_SYNTHETIC_ORIGIN_POLICY':'strict-typo'}
        with patch.dict(os.environ, settings, clear=True):
            with self.assertRaisesRegex(ValueError, 'origin policy'):
                dashboard_container.serve()

    def test_correction_source_policy_fails_closed_before_database_connection(self):
        for mode in ('typo', 'require_intake_txt', 'require_intake_fixture', 'require_intake_text_layer'):
            with self.subTest(mode=mode):
                settings = {'NAWA_INTAKE':'off', 'NAWA_AUTH':'oidc', 'NAWA_STORAGE':'postgres',
                            'NAWA_SYNTHETIC_CORRECTION_SOURCE':mode}
                with patch.dict(os.environ, settings, clear=True):
                    with self.assertRaises(ValueError):
                        dashboard_container.serve()

    @patch('nawa_demo.dashboard_container.HTTPServer')
    @patch('nawa_demo.dashboard_container.make_handler')
    @patch('nawa_demo.dashboard_container.DashboardState')
    def test_container_binds_inside_container(self, state_cls, handler, http_cls):
        http_cls.return_value.__enter__.return_value.serve_forever.return_value = None
        with contextlib.redirect_stdout(io.StringIO()):
            dashboard_container.serve()
        state_cls.assert_called_once_with()
        handler.assert_called_once_with(state_cls.return_value)
        http_cls.assert_called_once_with(('0.0.0.0', 8765), handler.return_value)
        http_cls.return_value.__enter__.return_value.serve_forever.assert_called_once_with()

    @patch('nawa_demo.dashboard_container.HTTPServer')
    def test_keyboard_interrupt_stops_cleanly(self, http_cls):
        http_cls.return_value.__enter__.return_value.serve_forever.side_effect = KeyboardInterrupt
        with contextlib.redirect_stdout(io.StringIO()):
            dashboard_container.serve()
        http_cls.return_value.__exit__.assert_called_once()

    def test_local_dashboard_binding_unmodified(self):
        # Prevent a mistaken replacement of the existing local-only launcher.
        import inspect
        from nawa_demo.dashboard import main
        self.assertIn("'127.0.0.1'", inspect.getsource(main))


if __name__ == '__main__':
    unittest.main()
