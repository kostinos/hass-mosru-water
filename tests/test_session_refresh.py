"""Exercise coordinator session expiry and recovery without running HA."""
import ast
import threading
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

SOURCE = Path(__file__).resolve().parents[1] / 'custom_components/mosru_water/coordinator.py'


class AuthError(Exception): pass
class TemporaryError(Exception): pass
class ApiError(Exception): pass
class ReauthRequired(Exception): pass


class SessionRefreshTest(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(SOURCE.read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
        cls.bases = []
        names = {'_prepare_client', '_fetch_device_info', '_fetch_device_info_unlocked', '_invalidate_client'}
        cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
        module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[
            ast.alias(name='annotations')], level=0), cls], type_ignores=[])
        self.clock = Mock(return_value=10000)
        ns = {'time': SimpleNamespace(monotonic=self.clock), 'ED_SESSION_REFRESH_SECONDS': 2400,
              'MosRuAuthError': AuthError, 'MosRuTemporaryError': TemporaryError,
              'MosRuApiError': ApiError, 'ConfigEntryAuthFailed': ReauthRequired,
              'UpdateFailed': ApiError, '_LOGGER': Mock()}
        for name in ('USER_PLACE_ID', 'PAYCODE', 'FLAT', 'COLD_ID', 'HOT_ID'):
            ns['CONF_' + name] = name.lower()
        exec(compile(ast.fix_missing_locations(module), str(SOURCE), 'exec'), ns)
        self.coordinator = ns['MosRuWaterCoordinator']()
        self.coordinator._io_lock = threading.Lock()
        self.client = Mock()
        self.client.try_refresh_acst.return_value = True
        self.client.get_device_info.return_value = {}
        self.coordinator._client = self.client
        self.coordinator._get_client = Mock(return_value=self.client)
        self.coordinator._get_effective_config = Mock(return_value={'user_place_id': 'place'})
        self.coordinator._ed_authorized_at = None

    def test_first_use_authorizes_and_caches_time(self):
        self.coordinator._prepare_client()
        self.client.authorize_ed.assert_called_once()
        self.assertEqual(self.coordinator._ed_authorized_at, 10000)

    def test_recent_session_is_reused(self):
        self.coordinator._ed_authorized_at = 9999
        self.coordinator._prepare_client()
        self.client.authorize_ed.assert_not_called()
        self.client.try_refresh_acst.assert_called_once()

    def test_session_renews_before_hour_expiry(self):
        self.coordinator._ed_authorized_at = 7600
        self.coordinator._prepare_client()
        self.client.authorize_ed.assert_called_once()

    def test_read_rejection_renews_once_then_retries_read(self):
        self.coordinator._ed_authorized_at = 9999
        self.client.get_device_info.side_effect = [AuthError(), {}]
        self.coordinator._fetch_device_info()
        self.client.authorize_ed.assert_called_once()
        self.assertEqual(self.client.get_device_info.call_count, 2)
        self.client.send_reading.assert_not_called()
        self.client.remove_last_indication.assert_not_called()

    def test_second_read_rejection_requests_qr_without_loop(self):
        self.coordinator._ed_authorized_at = 9999
        self.client.get_device_info.side_effect = AuthError()
        with self.assertRaises(ReauthRequired): self.coordinator._fetch_device_info()
        self.assertEqual(self.client.get_device_info.call_count, 2)
        self.assertIsNone(self.coordinator._client)
        self.assertIsNone(self.coordinator._ed_authorized_at)

    def test_refresh_rejection_requests_qr(self):
        self.client.authorize_ed.side_effect = AuthError()
        with self.assertRaises(ReauthRequired): self.coordinator._prepare_client()
        self.assertIsNone(self.coordinator._client)

    def test_transient_refresh_failure_does_not_request_qr(self):
        self.client.authorize_ed.side_effect = TemporaryError()
        with self.assertRaises(TemporaryError): self.coordinator._prepare_client()
        self.assertIs(self.coordinator._client, self.client)
        self.assertIsNone(self.coordinator._ed_authorized_at)

    def test_expired_sso_requests_qr_without_ed_request(self):
        self.client.try_refresh_acst.return_value = False
        with self.assertRaises(ReauthRequired): self.coordinator._prepare_client()
        self.client.authorize_ed.assert_not_called()

    def test_transient_sso_failure_does_not_request_qr(self):
        self.client.try_refresh_acst.side_effect = TemporaryError()
        with self.assertRaises(TemporaryError): self.coordinator._prepare_client()
        self.assertIs(self.coordinator._client, self.client)
