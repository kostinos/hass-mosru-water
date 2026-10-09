# Выбор квартиры и автоматический выбор счётчиков — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Мастер настройки перестаёт спрашивать код ЕПД и номер квартиры до входа: после QR-входа квартира выбирается из профиля «Электронного дома», а счётчики ХВС/ГВС назначаются автоматически по `typeName`.

**Architecture:** `api.py` получает метод `list_places()` и чистые функции `pick_counters`, `counters_of_type`, `place_label`. В `config_flow.py` шаг `user` сразу ведёт к QR, после входа добавляется шаг `place`, шаг `discover` автоматически выбирает счётчики. Повторная авторизация и координатор не меняются.

**Tech Stack:** Python 3, Home Assistant config flow, `requests`, `unittest` (тесты без HA: `api.py` грузится напрямую, методы мастера — через AST-загрузчик `tests/test_reauth.py::load_flow_methods`).

**Спецификация:** `docs/superpowers/specs/2026-10-06-place-and-counter-select-design.md`

---

## Подготовка

Рабочий каталог: `.claude/worktrees/feature-place-select` (ветка `feature-place-select`).

```bash
python3 -m venv .venv
.venv/bin/pip install requests qrcode
.venv/bin/python -m unittest discover -s tests
```

Ожидается: `Ran 70 tests ... OK`.

Каждое сообщение коммита в этом плане завершается строкой-трейлером (через пустую строку):

```
Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Результат проверки на живых данных (уже выполнена)

Ключи записи `getInfo` → `data.addresses[]` (значения не логировались):
`userPlaceId` (int), `fls` (str, 10 символов), `flat` (str), `addressCaption` (str — строка адреса дома),
`caption` (str — подпись адреса в «Электронном доме»), `localAddressCaption` (str), а также служебные поля.
В профиле владельца HA две квартиры. Значения `typeName` счётчиков: `ХВС`, `ГВС`.

Решение: подпись адреса — `addressCaption`, запасной вариант — `caption`.

## Карта файлов

| Файл | Что меняется |
|------|--------------|
| `custom_components/mosru_water/api.py` | `list_places()`; `find_user_place_id()` поверх него; функции `pick_counters`, `counters_of_type`, `place_label`, константы `COLD_TYPE`, `HOT_TYPE` |
| `custom_components/mosru_water/config_flow.py` | `async_step_user` без формы; новый `async_step_place`, `_load_places`, `_async_select_place`; переписан `async_step_discover`; удалён `_discover_counters`; маршрут после входа → `place` |
| `custom_components/mosru_water/strings.json`, `translations/ru.json`, `translations/en.json` | удалён `config.step.user`; добавлены `config.step.place`, `config.error.cannot_get_places`, `config.abort.no_places` |
| `tests/test_api.py` | тесты `list_places` и чистых функций |
| `tests/test_reauth.py` | загрузчик знает новые методы и имена |
| `tests/test_place_flow.py` | новый: тесты шагов `user`, `place`, `discover`, переводов |
| `README.md` | раздел «Настройка» |

---

### Task 1: `list_places()` и `find_user_place_id()` поверх него

**Files:**
- Modify: `custom_components/mosru_water/api.py` (метод `find_user_place_id`)
- Test: `tests/test_api.py`

- [ ] **Step 1: Write the failing test**

В `tests/test_api.py` после класса `FindUserPlaceIdTest` добавить:

```python
# Ответ getInfo с полями, которые нужны для выбора квартиры (ключи — из живого ответа).
_PLACES_PAYLOAD = {
    "data": {
        "addresses": [
            {"userPlaceId": 3395115, "fls": "1344364128", "flat": "46",
             "addressCaption": "ул. Тестовая, д. 1", "caption": "Дом"},
            {"userPlaceId": 999001, "fls": "1111111111", "flat": 5, "caption": "Дача"},
            # повтор той же квартиры
            {"userPlaceId": 3395115, "fls": "1344364128", "flat": "46"},
            # без userPlaceId — адресовать нельзя
            {"fls": "2222222222", "flat": "7"},
            {"userPlaceId": 777, "fls": None, "flat": None},
            "garbage",
        ],
    }
}


class ListPlacesTest(unittest.TestCase):
    def setUp(self):
        self.client = MosRuClient()
        self.session = mock.Mock()
        self.client._session = self.session
        self.session.request.return_value = FakeResponse(200, _PLACES_PAYLOAD)

    def test_requests_profile(self):
        self.client.list_places()
        method, url = self.session.request.call_args[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/profile/user/getInfo/"))

    def test_normalizes_and_deduplicates(self):
        self.assertEqual(self.client.list_places(), [
            {"user_place_id": "3395115", "paycode": "1344364128", "flat": "46",
             "address": "ул. Тестовая, д. 1"},
            {"user_place_id": "999001", "paycode": "1111111111", "flat": "5",
             "address": "Дача"},
            {"user_place_id": "777", "paycode": "", "flat": "", "address": ""},
        ])

    def test_empty_profile(self):
        self.session.request.return_value = FakeResponse(200, {"data": {}})
        self.assertEqual(self.client.list_places(), [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest tests.test_api.ListPlacesTest -v`
Expected: 3 errors `AttributeError: 'MosRuClient' object has no attribute 'list_places'`

- [ ] **Step 3: Write minimal implementation**

В `custom_components/mosru_water/api.py` заменить метод `find_user_place_id` целиком на:

```python
    def list_places(self) -> list[dict]:
        """Квартиры из профиля ed.mos.ru — для выбора в мастере настройки.

        Профиль перечисляет квартиры в data.addresses: fls (код плательщика),
        flat, userPlaceId и готовая строка адреса дома addressCaption.

        Returns: [{user_place_id, paycode, flat, address}] — все значения строки.
        """
        data = self._request_json(
            "GET",
            f"{_ED_API}/profile/user/getInfo/",
            headers=_XHR_HEADERS,
            retries=_RETRY_ATTEMPTS,
        )
        result: list[dict] = []
        seen: set[str] = set()
        for place in (data.get("data") or {}).get("addresses") or []:
            if not isinstance(place, dict) or not place.get("userPlaceId"):
                continue
            user_place_id = str(place["userPlaceId"])
            if user_place_id in seen:
                continue
            seen.add(user_place_id)
            result.append({
                "user_place_id": user_place_id,
                # flat и fls приходят то строкой, то числом
                "paycode": str(place.get("fls") or ""),
                "flat": str(place.get("flat") or ""),
                "address": str(place.get("addressCaption") or place.get("caption") or ""),
            })
        return result

    def find_user_place_id(self, paycode: str, flat: str) -> str:
        """Определить userPlaceId по коду плательщика и номеру квартиры.

        ed.mos.ru адресует квартиру своим userPlaceId, а не paycode. Нужен
        координатору: после «Выйти и войти заново» userPlaceId ищется заново
        по сохранённым реквизитам.
        """
        for place in self.list_places():
            if place["paycode"] != str(paycode):
                continue
            if flat and place["flat"] != str(flat):
                continue
            return place["user_place_id"]
        raise MosRuApiError(
            f"В профиле ed.mos.ru не найдена квартира с кодом плательщика {paycode}"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest tests.test_api -v`
Expected: все тесты `ListPlacesTest` и `FindUserPlaceIdTest` — `ok`, итог `OK`.

- [ ] **Step 5: Commit**

```bash
git add custom_components/mosru_water/api.py tests/test_api.py
git commit -m "Add list_places() for picking a flat from the ed.mos.ru profile"
```

---

### Task 2: чистые функции выбора счётчиков и подписи квартиры

**Files:**
- Modify: `custom_components/mosru_water/api.py` (модульный уровень, после `_parse_form`)
- Test: `tests/test_api.py`

- [ ] **Step 1: Write the failing test**

В `tests/test_api.py` после `ListPlacesTest` добавить:

```python
_COLD = {"id": "1", "name": "14-007378", "type": "ХВС"}
_HOT = {"id": "2", "name": "14-087265", "type": "ГВС"}


class PickCountersTest(unittest.TestCase):
    def test_one_cold_one_hot(self):
        self.assertEqual(api.pick_counters([_HOT, _COLD]), ("1", "2"))

    def test_two_cold_meters_are_ambiguous(self):
        cold2 = {"id": "3", "name": "x", "type": "ХВС"}
        self.assertEqual(api.pick_counters([_COLD, cold2, _HOT]), (None, "2"))

    def test_unknown_types(self):
        self.assertEqual(
            api.pick_counters([{"id": "5", "name": "x", "type": ""},
                               {"id": "6", "name": "y", "type": "ЭЛ"}]),
            (None, None),
        )

    def test_empty(self):
        self.assertEqual(api.pick_counters([]), (None, None))

    def test_type_case_and_spaces(self):
        self.assertEqual(
            api.pick_counters([{"id": "1", "name": "a", "type": " хвс "},
                               {"id": "2", "name": "b", "type": "гвс"}]),
            ("1", "2"),
        )


class CountersOfTypeTest(unittest.TestCase):
    def test_filters_by_type(self):
        self.assertEqual(api.counters_of_type([_COLD, _HOT], api.COLD_TYPE), [_COLD])

    def test_falls_back_to_all_when_type_missing(self):
        other = {"id": "9", "name": "z", "type": ""}
        self.assertEqual(api.counters_of_type([other], api.HOT_TYPE), [other])


class PlaceLabelTest(unittest.TestCase):
    def test_full(self):
        self.assertEqual(
            api.place_label({"user_place_id": "1", "paycode": "1344364128",
                             "flat": "46", "address": "ул. Тестовая, д. 1"}),
            "ул. Тестовая, д. 1, кв. 46 — ЕПД 1344364128",
        )

    def test_without_address(self):
        self.assertEqual(
            api.place_label({"user_place_id": "1", "paycode": "1344364128",
                             "flat": "46", "address": ""}),
            "кв. 46 — ЕПД 1344364128",
        )

    def test_only_id(self):
        self.assertEqual(
            api.place_label({"user_place_id": "777", "paycode": "", "flat": "", "address": ""}),
            "777",
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest tests.test_api.PickCountersTest tests.test_api.CountersOfTypeTest tests.test_api.PlaceLabelTest -v`
Expected: errors `AttributeError: module 'mosru_water_api' has no attribute 'pick_counters'` (и аналогичные).

- [ ] **Step 3: Write minimal implementation**

В `custom_components/mosru_water/api.py` сразу после функции `_parse_form` добавить:

```python
# Тип счётчика в ответе ed.mos.ru (поле typeName).
COLD_TYPE = "ХВС"
HOT_TYPE  = "ГВС"


def _counter_type(counter: dict) -> str:
    return str(counter.get("type") or "").strip().upper()


def counters_of_type(counters: list[dict], type_name: str) -> list[dict]:
    """Счётчики заданного типа; если таких нет — все, чтобы было из чего выбрать."""
    matched = [c for c in counters if _counter_type(c) == type_name]
    return matched or list(counters)


def pick_counters(counters: list[dict]) -> tuple[str | None, str | None]:
    """(cold_id, hot_id): единственный счётчик ХВС и единственный ГВС, иначе None."""
    def single(type_name: str) -> str | None:
        ids = [c["id"] for c in counters if _counter_type(c) == type_name]
        return ids[0] if len(ids) == 1 else None

    return single(COLD_TYPE), single(HOT_TYPE)


def place_label(place: dict) -> str:
    """Подпись квартиры в списке: «адрес, кв. N — ЕПД код»."""
    label = ", ".join(
        part for part in (
            place.get("address") or "",
            f"кв. {place['flat']}" if place.get("flat") else "",
        ) if part
    )
    if place.get("paycode"):
        label = f"{label} — ЕПД {place['paycode']}" if label else f"ЕПД {place['paycode']}"
    return label or place["user_place_id"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest tests.test_api -v`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add custom_components/mosru_water/api.py tests/test_api.py
git commit -m "Add helpers to pick ХВС/ГВС meters and label flats"
```

---

### Task 3: загрузчик тестов мастера знает новые методы

**Files:**
- Modify: `tests/test_reauth.py` (функция `load_flow_methods`)

- [ ] **Step 1: Обновить загрузчик**

В `tests/test_reauth.py`:

1. После `SOURCE = ...` добавить загрузку `api.py` (нужны настоящие чистые функции):

```python
import importlib.util

_API_PATH = SOURCE.parent / "api.py"
_spec = importlib.util.spec_from_file_location("mosru_water_api_for_flow", _API_PATH)
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)
```

2. Множество `names` в `load_flow_methods` заменить на:

```python
    names = {"__init__", "async_step_reauth", "_notify_qr_auth", "_poll_qr_scan",
             "async_step_qr", "async_step_code", "async_step_totp", "_async_submit_code",
             "async_step_user", "async_step_place", "_load_places", "_async_select_place",
             "async_step_discover"}
```

3. В словаре `namespace` строку с `"vol"` заменить на:

```python
        "vol": SimpleNamespace(Schema=lambda value: value,
                               Required=lambda key, **kw: key,
                               Optional=lambda key, **kw: key),
```

и добавить в тот же словарь:

```python
        "CONF_PAYCODE": "paycode", "CONF_FLAT": "flat",
        "CONF_USER_PLACE_ID": "user_place_id",
        "CONF_COLD_ID": "cold_counter_id", "CONF_HOT_ID": "hot_counter_id",
        "pick_counters": api.pick_counters,
        "counters_of_type": api.counters_of_type,
        "place_label": api.place_label,
        "COLD_TYPE": api.COLD_TYPE, "HOT_TYPE": api.HOT_TYPE,
```

4. Загрузчик должен пропускать имена, которых ещё нет в классе (новые методы появятся в задачах 4–6). Фильтр уже работает по `n.name in names`, менять его не нужно.

- [ ] **Step 2: Run tests**

Run: `.venv/bin/python -m unittest discover -s tests`
Expected: `OK` (поведение не изменилось).

- [ ] **Step 3: Commit**

```bash
git add tests/test_reauth.py
git commit -m "Let flow tests load place/discover steps and api helpers"
```

---

### Task 4: шаг `user` без формы и маршрут после входа в `place`

**Files:**
- Modify: `custom_components/mosru_water/config_flow.py` (`__init__`, `async_step_user`, `async_step_qr`, `_async_submit_code`)
- Create: `tests/test_place_flow.py`

- [ ] **Step 1: Write the failing test**

Создать `tests/test_place_flow.py`:

```python
"""Мастер: квартира из профиля «Электронного дома», счётчики по типу ХВС/ГВС."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from test_reauth import SOURCE, load_flow_methods

_PLACE_A = {"user_place_id": "3395115", "paycode": "1344364128", "flat": "46",
            "address": "ул. Тестовая, д. 1"}
_PLACE_B = {"user_place_id": "999001", "paycode": "1111111111", "flat": "5",
            "address": "Дача"}
_COLD = {"id": "1", "name": "14-007378", "type": "ХВС"}
_HOT = {"id": "2", "name": "14-087265", "type": "ГВС"}


class FlowTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cls, self.ns = load_flow_methods()
        self.ns["MosRuClient"].reset_mock()
        self.flow = cls()

        async def executor(fn, *args):
            return fn(*args)

        self.flow.hass = SimpleNamespace(
            async_add_executor_job=executor,
            config=SimpleNamespace(path=Mock(return_value="/tmp/www")),
        )
        self.flow._client = Mock()
        self.flow.async_show_form = Mock(return_value={"type": "form"})
        self.flow.async_abort = Mock(return_value={"type": "abort"})
        self.flow.async_step_sensors = AsyncMock(return_value={"type": "sensors"})

    def form_kwargs(self):
        return self.flow.async_show_form.call_args.kwargs


class UserStepTest(FlowTestBase):
    async def test_goes_straight_to_qr(self):
        self.flow._async_current_entries = Mock(return_value=[])
        self.flow.async_step_qr = AsyncMock(return_value={"type": "progress"})
        await self.flow.async_step_user()
        self.ns["MosRuClient"].assert_called_once()
        self.flow.async_step_qr.assert_awaited_once()
        self.flow.async_show_form.assert_not_called()

    async def test_single_entry_only(self):
        self.flow._async_current_entries = Mock(return_value=["entry"])
        await self.flow.async_step_user()
        self.flow.async_abort.assert_called_once_with(reason="already_configured")

    async def test_qr_success_leads_to_place(self):
        self.flow._client.get_session_cookies.return_value = {"c": "v"}
        self.flow._qr_task = asyncio.get_running_loop().create_future()
        self.flow._qr_task.set_result(True)
        self.flow.async_show_progress_done = Mock()
        await self.flow.async_step_qr()
        self.flow.async_show_progress_done.assert_called_once_with(next_step_id="place")

    async def test_code_success_leads_to_place(self):
        self.flow.async_step_place = AsyncMock(return_value={"type": "place"})
        await self.flow.async_step_totp({"sms_code": "123456"})
        self.flow.async_step_place.assert_awaited_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest discover -s tests -p "test_place_flow.py" -v` (через `discover -s tests`: файл импортирует `test_reauth` как модуль верхнего уровня)
Expected: `test_goes_straight_to_qr` FAIL (вызван `async_show_form`), `test_qr_success_leads_to_place` FAIL (`next_step_id="discover"`), `test_code_success_leads_to_place` FAIL/ERROR (нет `async_step_place`/вызван `async_step_discover`).

- [ ] **Step 3: Write minimal implementation**

В `custom_components/mosru_water/config_flow.py`:

1. В `__init__` после `self._counters_fetched: bool = False` добавить:

```python
        self._places: list[dict] | None = None
```

2. Заменить блок шага 1 (комментарий `# ── Шаг 1: код плательщика и квартира` и метод `async_step_user` целиком) на:

```python
    # ── Шаг 1: старт — сразу вход, реквизиты не спрашиваем ───────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Начать настройку: квартира выбирается из профиля после входа."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        self._client = MosRuClient()
        return await self.async_step_qr()
```

3. В `async_step_qr` заменить `return self.async_show_progress_done(next_step_id="discover")` на:

```python
        return self.async_show_progress_done(next_step_id="place")
```

4. В `_async_submit_code` заменить `return await self.async_step_discover()` на:

```python
                return await self.async_step_place()
```

5. Временная заглушка, чтобы маршрут работал до задачи 5 — **не добавлять**: `async_step_place` появится в задаче 5; тест `test_code_success_leads_to_place` подменяет его через `AsyncMock`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: `UserStepTest` — 4 × `ok`; остальные тесты — `ok`.

- [ ] **Step 5: Commit**

```bash
git add custom_components/mosru_water/config_flow.py tests/test_place_flow.py
git commit -m "Start setup with QR login and route new logins to flat selection"
```

---

### Task 5: шаг `place`

**Files:**
- Modify: `custom_components/mosru_water/config_flow.py` (импорт из `.api`; новые методы перед шагом счётчиков; удалить `_discover_counters`)
- Test: `tests/test_place_flow.py`

- [ ] **Step 1: Write the failing test**

В `tests/test_place_flow.py` добавить:

```python
class PlaceStepTest(FlowTestBase):
    def setUp(self):
        super().setUp()
        self.flow.async_step_discover = AsyncMock(return_value={"type": "discover"})

    async def test_single_place_is_selected_without_form(self):
        self.flow._client.list_places.return_value = [_PLACE_A]
        await self.flow.async_step_place()
        self.flow._client.authorize_ed.assert_called_once()
        self.flow.async_show_form.assert_not_called()
        self.assertEqual(self.flow._data, {
            "user_place_id": "3395115", "paycode": "1344364128", "flat": "46"})
        self.flow.async_step_discover.assert_awaited_once()

    async def test_several_places_show_choice(self):
        self.flow._client.list_places.return_value = [_PLACE_A, _PLACE_B]
        selector = self.ns["selector"]
        await self.flow.async_step_place()
        self.assertEqual(self.form_kwargs()["step_id"], "place")
        options = selector.SelectSelectorConfig.call_args.kwargs["options"]
        self.assertEqual(len(options), 2)
        labels = [c.kwargs["label"] for c in selector.SelectOptionDict.call_args_list]
        self.assertIn("ул. Тестовая, д. 1, кв. 46 — ЕПД 1344364128", labels)
        self.flow.async_step_discover.assert_not_awaited()

    async def test_choice_is_saved(self):
        self.flow._client.list_places.return_value = [_PLACE_A, _PLACE_B]
        await self.flow.async_step_place()
        await self.flow.async_step_place({"user_place_id": "999001"})
        self.assertEqual(self.flow._data, {
            "user_place_id": "999001", "paycode": "1111111111", "flat": "5"})
        self.flow.async_step_discover.assert_awaited_once()
        self.flow._client.list_places.assert_called_once()

    async def test_no_places_aborts(self):
        self.flow._client.list_places.return_value = []
        await self.flow.async_step_place()
        self.flow.async_abort.assert_called_once_with(reason="no_places")

    async def test_rejected_session_aborts(self):
        self.flow._client.authorize_ed.side_effect = self.ns["MosRuAuthError"]()
        await self.flow.async_step_place()
        self.flow.async_abort.assert_called_once_with(reason="session_expired")

    async def test_api_error_shows_reason_and_retries(self):
        self.flow._client.list_places.side_effect = [
            self.ns["MosRuApiError"]("ed.mos.ru auth: HTTP 451"), [_PLACE_A]]
        await self.flow.async_step_place()
        kwargs = self.form_kwargs()
        self.assertEqual(kwargs["errors"], {"base": "cannot_get_places"})
        self.assertEqual(kwargs["description_placeholders"],
                         {"error": "ed.mos.ru auth: HTTP 451"})
        await self.flow.async_step_place({})
        self.flow.async_step_discover.assert_awaited_once()
        self.assertEqual(self.flow._data["user_place_id"], "3395115")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest discover -s tests -p "test_place_flow.py" -v`
Expected: `PlaceStepTest` — ERROR `AttributeError: ... has no attribute 'async_step_place'`.

- [ ] **Step 3: Write minimal implementation**

В `custom_components/mosru_water/config_flow.py`:

1. Импорт из `.api` заменить на:

```python
from .api import (
    MosRuAuthError, MosRuApiError, MosRuClient,
    COLD_TYPE, HOT_TYPE, counters_of_type, pick_counters, place_label,
)
```

2. Удалить метод `_discover_counters` целиком. Перед строкой-комментарием `# ── Шаг 4: выбор счётчиков ──…` вставить:

```python
    # ── Шаг 4: выбор квартиры ────────────────────────────────────────────

    def _load_places(self) -> list[dict]:
        """Войти в ed.mos.ru и получить квартиры профиля (синхронно, в executor)."""
        self._client.authorize_ed()
        return self._client.list_places()

    async def async_step_place(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Выбор квартиры из профиля «Электронного дома»."""
        if user_input and self._places:
            chosen = {p["user_place_id"]: p for p in self._places}.get(
                user_input.get(CONF_USER_PLACE_ID)
            )
            if chosen is not None:
                return await self._async_select_place(chosen)

        if self._places is None:
            try:
                self._places = await self.hass.async_add_executor_job(self._load_places)
            except MosRuAuthError:
                return self.async_abort(reason="session_expired")
            except MosRuApiError as err:
                # Форма без полей: «Отправить» повторяет запрос.
                _LOGGER.error("Не удалось получить список квартир: %s", err)
                return self.async_show_form(
                    step_id="place",
                    data_schema=vol.Schema({}),
                    errors={"base": "cannot_get_places"},
                    description_placeholders={"error": str(err)},
                )

        if not self._places:
            return self.async_abort(reason="no_places")
        if len(self._places) == 1:
            return await self._async_select_place(self._places[0])

        return self.async_show_form(
            step_id="place",
            data_schema=vol.Schema({
                vol.Required(CONF_USER_PLACE_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=[
                        selector.SelectOptionDict(
                            value=p["user_place_id"], label=place_label(p)
                        )
                        for p in self._places
                    ])
                ),
            }),
        )

    async def _async_select_place(self, place: dict) -> FlowResult:
        """Запомнить квартиру: по userPlaceId идут все запросы к ed.mos.ru,
        paycode и flat нужны имени устройства и повторному поиску после выхода."""
        self._data[CONF_USER_PLACE_ID] = place["user_place_id"]
        self._data[CONF_PAYCODE] = place["paycode"]
        self._data[CONF_FLAT] = place["flat"]
        return await self.async_step_discover()

```

3. Комментарий `# ── Шаг 4: выбор счётчиков` переименовать в `# ── Шаг 5: выбор счётчиков`, `# ── Шаг 5: HA-сенсоры` — в `# ── Шаг 6: HA-сенсоры`.

4. В `async_step_discover` блок однократного запроса заменить (задача 6 перепишет шаг целиком; здесь — минимально, чтобы не было ссылки на удалённый `_discover_counters`):

```python
        if not self._counters_fetched:
            self._counters_fetched = True
            try:
                self._counters = await self.hass.async_add_executor_job(
                    self._client.get_counters, self._data[CONF_USER_PLACE_ID]
                )
            except MosRuAuthError:
                return self.async_abort(reason="session_expired")
            except MosRuApiError:
                self._counters = []  # падаем в ручной ввод
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add custom_components/mosru_water/config_flow.py tests/test_place_flow.py
git commit -m "Add flat selection step backed by the ed.mos.ru profile"
```

---

### Task 6: шаг `discover` — автоматический выбор и фильтр по типу

**Files:**
- Modify: `custom_components/mosru_water/config_flow.py` (`async_step_discover`)
- Test: `tests/test_place_flow.py`

- [ ] **Step 1: Write the failing test**

В `tests/test_place_flow.py` добавить:

```python
class DiscoverStepTest(FlowTestBase):
    def setUp(self):
        super().setUp()
        self.flow._data = {"user_place_id": "3395115"}

    async def test_cold_and_hot_are_assigned_automatically(self):
        self.flow._client.get_counters.return_value = [_HOT, _COLD]
        await self.flow.async_step_discover()
        self.flow._client.get_counters.assert_called_once_with("3395115")
        self.assertEqual(self.flow._data["cold_counter_id"], "1")
        self.assertEqual(self.flow._data["hot_counter_id"], "2")
        self.flow.async_show_form.assert_not_called()
        self.flow.async_step_sensors.assert_awaited_once()

    async def test_ambiguous_meters_are_filtered_and_prefilled(self):
        cold2 = {"id": "3", "name": "14-000001", "type": "ХВС"}
        self.flow._client.get_counters.return_value = [_COLD, cold2, _HOT]
        selector = self.ns["selector"]
        await self.flow.async_step_discover()
        self.assertEqual(self.form_kwargs()["step_id"], "discover")
        cold_opts, hot_opts = [
            c.kwargs["options"] for c in selector.SelectSelectorConfig.call_args_list[-2:]]
        self.assertEqual(len(cold_opts), 2)
        self.assertEqual(len(hot_opts), 1)
        suggested = self.flow._suggested_counters
        self.assertEqual(suggested, (None, "2"))

    async def test_user_choice_is_saved(self):
        self.flow._client.get_counters.return_value = [_COLD, _COLD | {"id": "3"}, _HOT]
        await self.flow.async_step_discover()
        await self.flow.async_step_discover(
            {"cold_counter_id": "3", "hot_counter_id": "2"})
        self.assertEqual(self.flow._data["cold_counter_id"], "3")
        self.flow.async_step_sensors.assert_awaited_once()

    async def test_api_error_falls_back_to_manual_ids(self):
        self.flow._client.get_counters.side_effect = self.ns["MosRuApiError"]()
        await self.flow.async_step_discover()
        self.assertEqual(self.form_kwargs()["step_id"], "discover")
        self.assertIn("Введите ID вручную",
                      self.form_kwargs()["description_placeholders"]["description"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest discover -s tests -p "test_place_flow.py" -v`
Expected: `test_cold_and_hot_are_assigned_automatically` FAIL (показана форма), `test_ambiguous_meters_are_filtered_and_prefilled` FAIL/ERROR (нет фильтра и `_suggested_counters`).

- [ ] **Step 3: Write minimal implementation**

В `custom_components/mosru_water/config_flow.py`:

1. В `__init__` после `self._places: list[dict] | None = None` добавить:

```python
        self._suggested_counters: tuple[str | None, str | None] = (None, None)
```

2. В `async_step_discover` заменить начало метода — от строки `"""Выбор счётчиков: ...` до конца блока `# ── Счётчики найдены автоматически` включительно (то есть до комментария `# ── Счётчики не найдены — ручной ввод`) — на:

```python
        """Выбор счётчиков: автоматически по типу ХВС/ГВС, из списка или вручную."""
        errors: dict[str, str] = {}

        # Однократно запрашиваем счётчики выбранной квартиры
        if not self._counters_fetched:
            self._counters_fetched = True
            try:
                self._counters = await self.hass.async_add_executor_job(
                    self._client.get_counters, self._data[CONF_USER_PLACE_ID]
                )
            except MosRuAuthError:
                return self.async_abort(reason="session_expired")
            except MosRuApiError:
                self._counters = []  # падаем в ручной ввод
            self._suggested_counters = pick_counters(self._counters)
            cold_id, hot_id = self._suggested_counters
            if cold_id and hot_id:
                # Обычная квартира: один ХВС и один ГВС — спрашивать нечего.
                self._data[CONF_COLD_ID] = cold_id
                self._data[CONF_HOT_ID] = hot_id
                return await self.async_step_sensors()

        # ── Счётчики найдены, но выбор неоднозначен ──────────────────────
        if self._counters:
            if user_input is not None:
                self._data[CONF_COLD_ID] = user_input[CONF_COLD_ID]
                self._data[CONF_HOT_ID]  = user_input[CONF_HOT_ID]
                return await self.async_step_sensors()

            def options(type_name: str) -> list:
                return [
                    selector.SelectOptionDict(
                        value=c["id"],
                        label=f"{c['name']} ({c['type']}, ID: {c['id']})",
                    )
                    for c in counters_of_type(self._counters, type_name)
                ]

            cold_id, hot_id = self._suggested_counters
            return self.async_show_form(
                step_id="discover",
                data_schema=vol.Schema({
                    vol.Required(
                        CONF_COLD_ID, description={"suggested_value": cold_id}
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options(COLD_TYPE))
                    ),
                    vol.Required(
                        CONF_HOT_ID, description={"suggested_value": hot_id}
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options(HOT_TYPE))
                    ),
                }),
                description_placeholders={
                    "description": "Выберите счётчики из вашего личного кабинета mos.ru",
                },
            )
```

Блок `# ── Счётчики не найдены — ручной ввод` и всё после него остаются без изменений.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add custom_components/mosru_water/config_flow.py tests/test_place_flow.py
git commit -m "Assign ХВС/ГВС meters automatically and filter the choice by type"
```

---

### Task 7: переводы

**Files:**
- Modify: `custom_components/mosru_water/strings.json`, `translations/ru.json`, `translations/en.json`
- Test: `tests/test_place_flow.py`

- [ ] **Step 1: Write the failing test**

В `tests/test_place_flow.py` добавить:

```python
class TranslationsTest(unittest.TestCase):
    def test_place_step_and_reasons(self):
        for name in ("strings.json", "translations/ru.json", "translations/en.json"):
            with self.subTest(translation=name):
                config = json.loads((SOURCE.parent / name).read_text())["config"]
                self.assertNotIn("user", config["step"])
                self.assertIn("user_place_id", config["step"]["place"]["data"])
                self.assertIn("{error}", config["error"]["cannot_get_places"])
                self.assertIn("no_places", config["abort"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest discover -s tests -p "test_place_flow.py" -v`
Expected: `TranslationsTest` FAIL (`'user' unexpectedly found`).

- [ ] **Step 3: Write minimal implementation**

Во всех трёх файлах:

1. Удалить объект `"user": {...}` из `config.step` целиком.
2. Добавить в `config.step` (перед `"discover"`):

`strings.json` и `translations/ru.json`:

```json
      "place": {
        "title": "Квартира",
        "description": "Квартиры из вашего профиля «Электронного дома» (ed.mos.ru).",
        "data": {
          "user_place_id": "Квартира"
        }
      },
```

`translations/en.json`:

```json
      "place": {
        "title": "Apartment",
        "description": "Apartments from your Electronic House (ed.mos.ru) profile.",
        "data": {
          "user_place_id": "Apartment"
        }
      },
```

3. Добавить в `config.error`:

ru: `"cannot_get_places": "Не удалось получить список квартир из «Электронного дома»: {error}. Нажмите «Отправить», чтобы повторить."`

en: `"cannot_get_places": "Could not get the apartment list from Electronic House: {error}. Press Submit to retry."`

4. Добавить в `config.abort`:

ru: `"no_places": "В профиле «Электронного дома» нет квартир. Добавьте квартиру на ed.mos.ru и начните настройку заново."`

en: `"no_places": "Your Electronic House profile has no apartments. Add one at ed.mos.ru and start the setup again."`

5. Проверить, что `strings.json` и `translations/ru.json` идентичны: `diff custom_components/mosru_water/strings.json custom_components/mosru_water/translations/ru.json` — пустой вывод.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add custom_components/mosru_water/strings.json custom_components/mosru_water/translations tests/test_place_flow.py
git commit -m "Translate flat selection step and its errors"
```

---

### Task 8: README

**Files:**
- Modify: `README.md` (раздел «## Настройка», подразделы «Шаг 1» … «Шаг 4»)

- [ ] **Step 1: Переписать раздел**

Заменить подразделы от `### Шаг 1 — Реквизиты` до конца `### Шаг 4 — Источники показаний` (не трогая `## Как отправляются показания`) на:

```markdown
### Шаг 1 — QR-авторизация
В интерфейсе HA отобразится QR-код. Откройте приложение **mos.ru** или **Госуслуги Москвы**, нажмите кнопку сканирования QR и направьте камеру на экран. После сканирования авторизация завершится автоматически.

Если на аккаунте mos.ru включена защита через приложение-аутентификатор, после сканирования мастер попросит ввести текущий 6-значный код из этого приложения.

### Шаг 2 — Квартира
Интеграция берёт квартиры из профиля [«Электронного дома»](https://ed.mos.ru/). Если квартира одна, она выбирается автоматически; если несколько — выберите нужную из списка. Код плательщика и номер квартиры вводить не нужно.

Если список пуст, добавьте квартиру в «Электронном доме» и начните настройку заново.

### Шаг 3 — Счётчики
Счётчики холодной (ХВС) и горячей (ГВС) воды определяются автоматически. Выбирать вручную нужно, только если в квартире несколько счётчиков одного типа.

### Шаг 4 — Источники показаний
- Выберите HA-сенсоры, откуда брать показания. Единица измерения должна быть `м³`
- Укажите день месяца для автоматической отправки (1–28)
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "Document flat selection and automatic meter assignment"
```

---

### Task 9: проверка целиком и на живом HA

- [ ] **Step 1: Полный прогон тестов и статическая проверка**

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/pip install -q pyflakes && .venv/bin/python -m pyflakes custom_components/mosru_water/api.py custom_components/mosru_water/config_flow.py
```

Expected: `OK`; pyflakes без вывода.

- [ ] **Step 2: Выложить на HA и проверить, что работающая запись не сломалась**

```bash
for f in api.py config_flow.py strings.json translations/ru.json translations/en.json; do
  ssh ha "cat > /config/custom_components/mosru_water/$f" < custom_components/mosru_water/$f
done
ssh ha 'ha core restart'
```

Через 1–2 минуты:

```bash
ssh ha 'ha core logs | grep -i mosru_water | grep -v "has not been tested" | tail -20'
```

Expected: нет `ERROR`/`Traceback` от `mosru_water`; запрос повторной авторизации не появился.

- [ ] **Step 3: Полный прогон мастера — только с согласия владельца HA**

Требует удалить запись интеграции и добавить заново (теряется история сенсоров «отправлено»). Если владелец согласен: удалить интеграцию → Добавить → QR (+код) → убедиться, что показан список из двух квартир с адресами → выбрать квартиру → шаг счётчиков пропущен → выбрать сенсоры → запись создана, сенсоры «(mos.ru)» показывают значения.
