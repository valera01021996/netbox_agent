# NetBox IPMI Move Auditor

Агент мониторинга, который отслеживает **фактическое местоположение IPMI/BMC интерфейсов** серверов и сравнивает с **ожидаемым подключением в NetBox**.

Если MAC-адрес IPMI обнаружен на другом коммутаторе или порту — агент создаёт алерт.

## Зачем это нужно?

- Сервер физически переехал в другую стойку, но NetBox не обновили
- Кабель IPMI случайно переключили в другой порт
- Ошибка при монтаже нового оборудования
- Аудит соответствия документации реальности

## Как это работает

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   NetBox    │────▶│   Agent     │◀────│  Switches   │
│  (Expected) │     │ (Compare)   │     │ (FDB/SNMP)  │
└─────────────┘     └──────┬──────┘     └─────────────┘
                           │
                           ▼
                    ┌─────────────┐
                    │  Alert via  │
                    │  Tag + Hook │
                    └─────────────┘
```

1. **Получает из NetBox** список серверов с OOB IP (IPMI/iLO/iDRAC) и их ожидаемое подключение (cable)
2. **Собирает FDB** (MAC-таблицы) с коммутаторов через SNMP
3. **Сравнивает** ожидаемое vs фактическое местоположение MAC
4. **При несоответствии** — ставит тег `ipmi-moved` на устройство в NetBox
5. **NetBox Webhook** отправляет алерт в KeepHQ (или другую систему)

## Требования

- Python 3.11+
- NetBox 3.5+ с API токеном (права: read devices/interfaces/cables, write tags/journal)
- Коммутаторы с SNMP v2c доступом
- Docker (опционально)

## Быстрый старт

### 1. Настройка окружения

```bash
cp .env.example .env
# Отредактируйте .env - укажите NETBOX_URL, NETBOX_TOKEN, SNMP_COMMUNITY
```

### 2. Запуск через Docker

```bash
docker-compose up -d
docker-compose logs -f
```

### 3. Запуск локально (для разработки)

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m netbox_ipmi_agent
```

## Конфигурация

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `NETBOX_URL` | — | URL NetBox API |
| `NETBOX_TOKEN` | — | API токен с правами на чтение и запись тегов |
| `NETBOX_VERIFY_SSL` | `true` | Проверка SSL сертификата |
| `SWITCHES_SELECTOR` | `role:switch` | Фильтр коммутаторов (role:X или tag:Y) |
| `POLL_INTERVAL` | `300` | Интервал проверки (секунды) |
| `MOVE_CONFIRM_RUNS` | `2` | Сколько циклов подряд для подтверждения переезда |
| `SNMP_COMMUNITY` | `public` | SNMP community string |
| `SNMP_VERSION` | `2c` | Версия SNMP |
| `SNMP_TIMEOUT` | `5` | Таймаут SNMP (секунды) |
| `MOVE_TAG_NAME` | `ipmi-moved` | Имя тега для алертов |
| `REMIND_AFTER` | `6h` | Повторное напоминание через |
| `LOG_LEVEL` | `INFO` | Уровень логирования |

## Интеграция с KeepHQ

### Шаг 1: Создать API Key в KeepHQ

1. Зайти в **KeepHQ**
2. Нажать на **иконку пользователя** (правый верхний угол)
3. Выбрать **Settings**
4. Перейти в раздел **API Keys**
5. Нажать **Create API Key**:
   - **Name:** `NetBox Webhook`
   - **Role:** `webhook`
6. **Скопировать и сохранить API Key** — он понадобится на следующем шаге

### Шаг 2: Создать Webhook в NetBox

1. Зайти в **NetBox** → **Operations** → **Integrations** → **Webhooks**
2. Нажать **Add**
3. Заполнить поля:

| Поле | Значение |
|------|----------|
| **Name** | `KeepHQ Alerts` |
| **URL** | `http://<KEEP_HOST>:8080/alerts/event/netbox` |
| **HTTP method** | `POST` |
| **HTTP content type** | `application/json` |

4. В поле **Additional headers** добавить:
```
X-API-KEY: <ВАШ_API_KEY_ИЗ_KEEP>
```

5. **Убрать галочку** `SSL verification` (если KeepHQ без HTTPS)

6. Нажать **Create**

### Шаг 3: Создать Event Rule в NetBox

1. Зайти в **NetBox** → **Operations** → **Integrations** → **Event Rules**
2. Нажать **Add**
3. Заполнить поля:

| Поле | Значение |
|------|----------|
| **Name** | `IPMI Move to KeepHQ` |
| **Object types** | `DCIM > Device` |
| **Event types** | ☑️ `Object created` ☑️ `Object updated` ☑️ `Object deleted` |
| **Action type** | `Webhook` |
| **Webhook** | Выбрать `KeepHQ Alerts` (созданный на шаге 2) |

4. Нажать **Create**

### Шаг 4: Проверить интеграцию

1. Вручную добавьте тег `ipmi-moved` на любое устройство в NetBox
2. Проверьте, что алерт появился в **KeepHQ** → **Feed**
3. Удалите тег с устройства

> **Примечание:** Порт `8080` — это backend-контейнер KeepHQ. Если используете другой порт или reverse proxy, укажите соответствующий URL.

## Логика работы

### Статусы

| Статус | Описание | Действие |
|--------|----------|----------|
| `OK` | MAC на ожидаемом switch:port | Удаляет тег `ipmi-moved` |
| `OK_MLAG_PEER` | MAC на MLAG-паре (норма) | Удаляет тег |
| `MOVE_DETECTED` | MAC на другом месте | Счётчик +1 |
| `MOVE_CONFIRMED` | Подтверждено N циклов | Ставит тег `ipmi-moved` |
| `SUSPECT_UPLINK` | MAC на uplink-порту | Не считается переездом |
| `NOT_FOUND` | MAC не найден в FDB | Логируется |

### Подтверждение переезда

Чтобы избежать ложных срабатываний из-за MAC aging или флапов:

- Переезд считается подтверждённым после `MOVE_CONFIRM_RUNS` (по умолчанию 2) последовательных наблюдений
- Только после подтверждения ставится тег и отправляется алерт

### Дедупликация алертов

- Повторный алерт по тому же переезду отправляется через `REMIND_AFTER` (по умолчанию 6 часов)
- При возврате на место — тег удаляется

## Логи

```bash
# Docker
docker-compose logs -f

# Примеры логов
{"event": "Move detected, waiting for confirmation", "counter": 1, "threshold": 2}
{"event": "Move CONFIRMED after 2 consecutive observations", "server": "srv01"}
{"event": "Added tag to device", "device": "srv01", "tag": "ipmi-moved"}
```

## Troubleshooting

### Тег не ставится

1. Проверьте права токена: нужны `dcim.change_device` и `extras.add_tag`
2. Проверьте логи на ошибки `Failed to add tag`

### Webhook не срабатывает

1. Проверьте URL webhook в NetBox
2. Проверьте Conditions — попробуйте убрать условие для теста
3. Посмотрите **NetBox** → **Integrations** → **Webhooks** → выберите webhook → **Recent Activity**

### SNMP не работает

1. Проверьте доступность: `snmpwalk -v2c -c public SWITCH_IP 1.3.6.1.2.1.1.1`
2. Проверьте firewall и ACL на коммутаторах
3. Увеличьте `SNMP_TIMEOUT` и `SNMP_RETRIES`

### MAC не находится в FDB

- MAC может быть не выучен (сервер выключен, нет трафика)
- Проверьте VLAN — MAC может быть в другом VLAN
- Проверьте что коммутатор в списке `SWITCHES_SELECTOR`

## Разработка

```bash
# Установка dev-зависимостей
pip install -r requirements-dev.txt

# Запуск тестов
pytest

# Линтинг
ruff check .
```

## Лицензия

MIT
