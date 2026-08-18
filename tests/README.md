# Тесты

Проверяют клиент `ed.mos.ru`: разбор ответов портала, форму запросов и
обработку отказов. Сеть не используется — `requests.Session` подменяется
заглушкой, так что тесты не зависят от состояния портала и сессии.

Ответы в фикстурах взяты из реальных HAR-записей: тесты проверяют фактические
форматы `ed.mos.ru`, а не представление о них.

## Запуск

Нужен только `requests` (его импортирует `api.py`); Home Assistant не требуется.

```bash
python3 -m venv .venv
.venv/bin/pip install requests
.venv/bin/python -m unittest discover -s tests -v
```

С pytest, если он установлен:

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -q
```

## Что покрыто

| Группа | Проверяет |
|---|---|
| `PeriodTest` | `period` = конец расчётного месяца (включая февраль и высокосный год) |
| `ParseResponseTest` | 404 не считается успехом, «уже внесено» → отдельное исключение, 5xx/`retry_later` → временная ошибка, 401/403 → ошибка авторизации |
| `ClientCallsTest` | `PUT addIndications` с целым `indication`, `DELETE removeLastValue`, отсутствие ретраев при отправке |
| `CountersParsingTest` | разбор `listByPayerCode`, инверсия `enableTransfer` → `readonly` |
| `FindUserPlaceIdTest` | поиск `userPlaceId` в `data.addresses`, сравнение `flat` как строки (портал отдаёт число) |
| `AuthorizeEdTest` | OAuth: извлечение `code`, обмен на сессию, истёкшая сессия |

Ключевой регресс, который они закрывают — `test_404_is_error_not_success`:
интеграция «успешно отправляла» показания на несуществующий эндпоинт с самого
первого релиза, потому что 404 не проверялся.

Отсутствие ретраев в `send_reading` (`test_send_reading_does_not_retry`) тоже
принципиально: повторный `PUT` может создать дубль показания.
