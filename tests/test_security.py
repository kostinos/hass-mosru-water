"""Security regressions; real methods with synthetic sensors/HTTP and no live writes."""
from __future__ import annotations
import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import functools
import importlib.util
import io
import logging
from pathlib import Path
import re
import secrets
import os
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import requests
from test_api import api

ROOT = Path(__file__).parents[1] / 'custom_components' / 'mosru_water'
spec = importlib.util.spec_from_file_location('security_constants', ROOT / 'const.py')
const = importlib.util.module_from_spec(spec)
spec.loader.exec_module(const)
tree = ast.parse((ROOT / 'coordinator.py').read_text())
cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
cls.bases = []
cls.body = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name != '__init__']
namespace = {key: value for key, value in vars(const).items() if not key.startswith('__')}
namespace.update(dict(threading=threading, time=time, functools=functools,
    datetime=datetime, timedelta=timedelta, _LOGGER=logging.getLogger(__name__),
    dt_util=SimpleNamespace(now=lambda: datetime.now(timezone.utc)),
    MosRuClient=api.MosRuClient, MosRuAuthError=api.MosRuAuthError,
    MosRuApiError=api.MosRuApiError, MosRuTemporaryError=api.MosRuTemporaryError,
    MosRuAlreadySubmittedError=api.MosRuAlreadySubmittedError,
    normalized_reading=api.normalized_reading, UpdateFailed=type('UpdateFailed', (Exception,), {}),
    ConfigEntryAuthFailed=type('ConfigEntryAuthFailed', (Exception,), {})))
module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), cls], type_ignores=[])
exec(compile(ast.fix_missing_locations(module), str(ROOT / 'coordinator.py'), 'exec'), namespace)


class ReadingSafetyTests(unittest.TestCase):
    def setUp(self):
        self.coordinator = namespace['MosRuWaterCoordinator']()
        self.coordinator._io_lock = threading.Lock()
        self.coordinator._last_write_at = float('-inf')
        self.coordinator._manual_pending = False
        self.coordinator._submitted_month = None
        self.coordinator._invalidate_client = Mock()
        self.config = {const.CONF_COLD_ENTITY: 'sensor.cold', const.CONF_HOT_ENTITY: 'sensor.hot',
                       const.CONF_COLD_ID: 'cold', const.CONF_HOT_ID: 'hot'}
        self.coordinator._get_effective_config = Mock(return_value=self.config)
        now = datetime.now(timezone.utc)
        self.states = {entity: SimpleNamespace(state=value, attributes={'unit_of_measurement': 'm³'},
            last_reported=now, last_updated=now) for entity, value in [('sensor.cold', '105.6'), ('sensor.hot', '52.2')]}
        self.coordinator.hass = SimpleNamespace(states=SimpleNamespace(get=self.states.get))
        self.client = Mock()
        self.info = {'cold': {'current_reading': 100, 'readonly': False,
            'reading_period': now.strftime('%Y-%m-01')}, 'hot': {'current_reading': 50,
            'readonly': False, 'reading_period': now.strftime('%Y-%m-01')}}
        self.client.get_device_info.return_value = self.info
        self.coordinator._prepare_client = Mock(return_value=(self.client, 'synthetic-place'))

    def assert_no_writes(self):
        self.client.send_reading.assert_not_called()
        self.client.remove_last_indication.assert_not_called()

    def test_invalid_values_never_reach_network(self):
        for value in ('nan', 'inf', '-1', '1e100', 'unknown'):
            with self.subTest(value=value):
                self.coordinator._last_write_at = float('-inf')
                self.states['sensor.hot'].state = value
                with self.assertRaises((namespace['UpdateFailed'], api.MosRuApiError)):
                    self.coordinator._submit(replace=True)
                self.assert_no_writes()
        self.coordinator._prepare_client.assert_not_called()

    def test_stale_sensor_and_changed_unit_rejected(self):
        self.states['sensor.hot'].last_reported -= timedelta(days=3)
        with self.assertRaises(namespace['UpdateFailed']):
            self.coordinator._submit()
        self.assert_no_writes()
        self.coordinator._last_write_at = float('-inf')
        self.states['sensor.hot'].last_reported = datetime.now(timezone.utc)
        self.states['sensor.hot'].attributes['unit_of_measurement'] = 'L'
        with self.assertRaises(namespace['UpdateFailed']):
            self.coordinator._submit()
        self.assert_no_writes()

    def test_both_counters_validated_before_first_delete(self):
        for changed in ({'readonly': True}, {'current_reading': None},
                        {'current_reading': 500}, {'current_reading': -1000},
                        {'reading_period': '2000-01-31'}):
            with self.subTest(changed=changed):
                original = dict(self.info['hot'])
                self.info['hot'].update(changed)
                self.coordinator._last_write_at = float('-inf')
                with self.assertRaises(namespace['UpdateFailed']):
                    self.coordinator._submit(replace=True)
                self.assert_no_writes()
                self.info['hot'] = original

    def test_unknown_or_duplicate_targets_rejected(self):
        self.info.pop('hot')
        with self.assertRaises(namespace['UpdateFailed']):
            self.coordinator._submit(replace=True)
        self.assert_no_writes()
        self.coordinator._last_write_at = float('-inf')
        self.config[const.CONF_HOT_ENTITY] = self.config[const.CONF_COLD_ENTITY]
        with self.assertRaises(namespace['UpdateFailed']):
            self.coordinator._submit(replace=True)
        self.assert_no_writes()

    def test_sent_audit_values_match_rounded_payload(self):
        result = self.coordinator._submit()
        self.assertEqual(result['last_cold'], 106)
        self.assertEqual(result['last_hot'], 52)
        self.assertEqual([call.args[2] for call in self.client.send_reading.call_args_list], [106, 52])
        self.assertEqual(result['last_status'], 'success')

    def test_unknown_put_after_delete_reports_partial_and_stops(self):
        self.client.send_reading.side_effect = api.MosRuTemporaryError('synthetic timeout')
        result = self.coordinator._submit(replace=True)
        self.assertEqual(result['last_status'], 'partial')
        self.assertEqual(result['operation_results']['холодная']['stage'], 'send_unknown_after_delete')
        self.client.remove_last_indication.assert_called_once_with('synthetic-place', 'cold')
        self.assertNotIn('last_cold', result)

    def test_partial_second_write_keeps_only_confirmed_audit(self):
        self.client.send_reading.side_effect = [{}, api.MosRuTemporaryError('synthetic timeout')]
        result = self.coordinator._submit()
        self.assertEqual(result['last_status'], 'partial')
        self.assertEqual(result['last_cold'], 106)
        self.assertNotIn('last_hot', result)
        self.client.remove_last_indication.assert_not_called()

    def test_already_submitted_is_not_a_new_sent_reading(self):
        self.client.send_reading.side_effect = api.MosRuAlreadySubmittedError()
        result = self.coordinator._submit()
        self.assertEqual(result['last_status'], 'already_submitted')
        self.assertNotIn('last_cold', result)
        self.assertNotIn('last_submitted_at', result)

    def test_current_closed_period_is_already_submitted_without_put(self):
        for info in self.info.values():
            info['readonly'] = True
        result = self.coordinator._submit()
        self.assertEqual(result['last_status'], 'already_submitted')
        self.assert_no_writes()

    def test_partial_guard_is_persisted_and_cleared_only_after_confirmed_write(self):
        self.coordinator._entry = SimpleNamespace(data={})
        updates = []
        def update(entry, *, data):
            entry.data = data
            updates.append(data)
        self.coordinator.hass.config_entries = SimpleNamespace(async_update_entry=update)
        self.coordinator._persist_operation_guard({'last_status': 'partial', 'operation_results': {'cold': 'unknown'}})
        self.assertEqual(updates[-1]['submission_blocked_month'], self.coordinator._current_month())
        self.coordinator._persist_operation_guard({'last_status': 'success'})
        self.assertNotIn('submission_blocked_month', updates[-1])

    def test_concurrent_replace_and_poll_do_not_share_session(self):
        started, release = threading.Event(), threading.Event()
        def send(*args):
            started.set()
            self.assertTrue(release.wait(2))
            return {}
        self.client.send_reading.side_effect = send
        with ThreadPoolExecutor(max_workers=2) as executor:
            future = executor.submit(self.coordinator._submit, replace=True)
            self.assertTrue(started.wait(2))
            try:
                with self.assertRaises(namespace['UpdateFailed']):
                    self.coordinator._submit(replace=True)
                with self.assertRaises(api.MosRuTemporaryError):
                    self.coordinator._fetch_device_info()
                self.assertEqual(self.client.remove_last_indication.call_count, 1)
            finally:
                release.set()
            future.result()
        self.assertEqual(self.client.remove_last_indication.call_count, 2)

    def test_writes_have_cooldown(self):
        self.coordinator._submit()
        with self.assertRaises(namespace['UpdateFailed']):
            self.coordinator._submit(replace=True)
        self.client.remove_last_indication.assert_not_called()


class HttpBoundaryTests(unittest.TestCase):
    def test_urls_reject_untrusted_origins(self):
        for url in ('http://login.mos.ru/x', 'https://login.mos.ru.evil.example/x',
                    'https://127.0.0.1/x', 'https://user:pass@login.mos.ru/x',
                    'https://login.mos.ru:8443/x', 'https://login.mos.ru/x\n',
                    'https://login.mos.ru/x)[injected](https://evil.example)'):
            with self.subTest(url=url), self.assertRaises(api.MosRuApiError):
                api.trusted_url(url)
        self.assertEqual(api.trusted_url('https://login.mos.ru/qr/mm/?code=test', qr=True),
                         'https://login.mos.ru/qr/mm/?code=test')
        with self.assertRaises(api.MosRuApiError):
            api.trusted_url('https://www.mos.ru/qr', qr=True)

    def test_redirect_is_checked_before_second_request(self):
        calls = []
        class Adapter(requests.adapters.BaseAdapter):
            def send(self, request, **kwargs):
                calls.append(request.url)
                response = requests.Response()
                response.status_code = 302
                response.headers['Location'] = 'https://evil.example/steal'
                response.url = request.url
                response.request = request
                response.raw = io.BytesIO(b'')
                return response
            def close(self): pass
        session = api.MosRuSession()
        session.mount('https://', Adapter())
        with self.assertRaises(api.MosRuApiError):
            session.post('https://login.mos.ru/sps/login/methods/sms', data={'sms-code': 'synthetic'})
        self.assertEqual(calls, ['https://login.mos.ru/sps/login/methods/sms'])

    def test_response_limit(self):
        response = requests.Response()
        response.raw = io.BytesIO(b'x' * (api._MAX_RESPONSE_BYTES + 1))
        with self.assertRaises(requests.RequestException):
            api.MosRuSession._bounded_response(response)

    def test_poll_does_not_log_redirect_tokens_or_html(self):
        client = api.MosRuClient()
        response = Mock(status_code=302, headers={'Location': 'https://login.mos.ru/?code=SECRET'})
        client._session = Mock()
        client._session.get.return_value = response
        with self.assertLogs(api._LOGGER, level='ERROR') as captured:
            with self.assertRaises(api.MosRuApiError) as error:
                client.poll_qr()
        self.assertNotIn('SECRET', str(error.exception) + ''.join(captured.output))


class QrArtifactTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / 'config_flow.py').read_text())
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in ('_write_qr_svg', '_delete_qr_svg')]
        module = ast.Module(body=nodes, type_ignores=[])
        self.namespace = dict(os=os, re=re, secrets=secrets, time=time,
            trusted_url=api.trusted_url, _QR_FILE='mosru_water_qr.svg', _LOGGER=Mock())
        exec(compile(ast.fix_missing_locations(module), 'qr_artifacts', 'exec'), self.namespace)

    def test_random_per_flow_files_cleanup_and_no_cross_flow_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / 'mosru_water_qr.svg'
            legacy.write_text('obsolete')
            urls = [self.namespace['_write_qr_svg'](directory, 'https://login.mos.ru/qr/mm/?code=test', 1)
                    for _ in range(2)]
            self.assertNotEqual(urls[0], urls[1])
            self.assertFalse(legacy.exists())
            self.assertEqual(len(list(Path(directory).glob('*.svg'))), 2)
            self.namespace['_delete_qr_svg'](directory, urls[0])
            self.assertEqual(len(list(Path(directory).glob('*.svg'))), 1)
            self.namespace['_delete_qr_svg'](directory, '/local/../unrelated.svg')
            self.assertEqual(len(list(Path(directory).glob('*.svg'))), 1)
            self.namespace['_delete_qr_svg'](directory, urls[1])
            self.assertFalse(list(Path(directory).glob('*.svg')))


class ServiceAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unload_refuses_running_portal_operation(self):
        tree = ast.parse((ROOT / '__init__.py').read_text())
        method = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                      and node.name == 'async_unload_entry')
        module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), method], type_ignores=[])
        ns = dict(DOMAIN='mosru_water', PLATFORMS=['sensor', 'button'],
                  SERVICE_REPLACE_READINGS='replace_readings')
        exec(compile(ast.fix_missing_locations(module), 'unload_entry', 'exec'), ns)
        lock = threading.Lock()
        coordinator = SimpleNamespace(_io_lock=lock, _manual_pending=False)
        hass = SimpleNamespace(data={'mosru_water': {'entry': coordinator}},
            config_entries=SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True)),
            services=SimpleNamespace(async_remove=Mock()))
        with lock:
            self.assertFalse(await ns['async_unload_entry'](hass, SimpleNamespace(entry_id='entry')))
        hass.config_entries.async_unload_platforms.assert_not_called()
        self.assertTrue(await ns['async_unload_entry'](hass, SimpleNamespace(entry_id='entry')))

    async def test_confirmation_and_admin_registration(self):
        tree = ast.parse((ROOT / '__init__.py').read_text())
        method = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                      and node.name == '_async_register_services')
        module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), method], type_ignores=[])
        registration = Mock()
        validation = type('ServiceValidationError', (Exception,), {})
        ns = dict(DOMAIN='mosru_water', SERVICE_REPLACE_READINGS='replace_readings', ATTR_ENTRY_ID='entry_id',
                  _REPLACE_SCHEMA=None, async_register_admin_service=registration, ServiceValidationError=validation)
        exec(compile(ast.fix_missing_locations(module), 'service_registration', 'exec'), ns)
        coordinator = SimpleNamespace(async_replace_readings=AsyncMock(return_value={'last_status': 'success'}),
            data={}, async_set_updated_data=Mock())
        hass = SimpleNamespace(services=SimpleNamespace(has_service=lambda *args: False),
                               data={'mosru_water': {'entry': coordinator}})
        ns['_async_register_services'](hass)
        handler = registration.call_args.args[3]
        with self.assertRaises(validation):
            await handler(SimpleNamespace(data={}))
        coordinator.async_replace_readings.assert_not_called()
        await handler(SimpleNamespace(data={'confirm': True}))
        coordinator.async_replace_readings.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
