# Personal Ring Sync · Oura CLI

Переносимый CLI для Oura API v2: отдельные владельцы, вход с телефона через OAuth2/PKCE, автоматическое обновление токенов, SQLite-история, синхронизация, диагностика и подписанные вебхуки. Python 3.11+, Mac/Linux, без сторонних runtime-зависимостей.

Независимый проект. Не официальный MCP Oura и не продукт Ōura.

## Установка

```sh
python3 -m venv .venv
.venv/bin/pip install ./oura_personal_cli-0.1.0-py3-none-any.whl
.venv/bin/oura --help
```

Или из исходников: `pip install .`. Для разработки: `pip install -r requirements-dev.txt`, затем `pip install -e .`.

## Владелец и существующий доступ

```sh
oura profile create me --timezone Asia/Dubai
oura auth import-legacy --env-file /private/path/old.env
oura doctor --live --json
oura sync --json
oura sleep --date 2026-11-24 --json
```

Миграция существующего access token не создаёт refresh token. Для долговременного подключения нужен OAuth2. `--owner NAME` обязателен, когда в одном хранилище больше одного владельца; его можно поставить до или после команды. Разные установки используют независимые каталоги состояния.

По умолчанию состояние: `~/.local/share/oura-cli/`; переопределение: `OURA_CLI_HOME` или `--home`. Внутри `owners/NAME/`: настройки, секреты и SQLite. Каталоги 0700, секреты/данные 0600; `flock` и атомарная запись защищают refresh от одновременных запросов. Это разделение аккаунтов в приложении, а не защита от root/владельца той же ОС. Для разных людей рекомендуются отдельные системные пользователи или контейнеры.

## Вход с телефона

Для мобильного браузера используется HTTPS relay, запущенный по инструкции ниже. Регистрируется OAuth-приложение Oura; его redirect URI должен точно совпадать с `https://YOUR-RELAY/oauth/callback`.

Создайте вне репозитория приватный JSON (0600):

```json
{
  "client_id": "YOUR_APP_ID",
  "client_secret": "YOUR_APP_SECRET",
  "relay_url": "https://YOUR-RELAY",
  "relay_key": "YOUR_PRIVATE_ENROLLMENT_KEY"
}
```

```sh
oura auth configure --file /private/path/app.json
oura auth start --json
# Откройте authorize_url на телефоне, войдите в Oura и разрешите доступ.
oura auth finish --json
```

`auth start` выдаёт ссылку без секретов; код/пароль/токены в чат копировать не надо. После согласия Oura возвращает код в HTTPS callback, CLI забирает его по отдельному секрету и обменивает у Oura. PKCE verifier остаётся на запускающем CLI. Relay не обменивает коды и не получает access/refresh tokens. Коды живут не более 10 минут; успешный вход удаляет сессию. При `pending` можно повторить `auth finish`; `auth cancel` отменяет локальную попытку. `auth login --no-browser` объединяет start/ожидание/finish для терминала. Вход одного аккаунта не может заменить уже закреплённый другой аккаунт.

Неоднозначный сетевой результат обмена одноразового refresh token не повторяется вслепую: CLI сохраняет отметку `refresh_uncertain` и требует нового входа. `auth logout --confirm-owner NAME` убирает локальный grant, сохраняя историю; отзыв доступа на стороне Oura выполняется в её интерфейсе.

## Данные и синхронизация

```sh
oura status --json
oura doctor --live --json
oura sync --endpoint sleep --endpoint daily_readiness --start 2026-11-01 --end 2026-11-24 --json
oura readiness --date 2026-11-24 --json
oura read daily_stress --start 2026-11-01 --end 2026-11-24 --json
oura battery --json
oura vo2 --json
oura export daily_sleep --format csv --output /private/path/sleep.csv
oura import-archive --directory /private/path/old-daily-snapshots
```

Первый `sync` получает примерно 30 дней. Последующие обновляют последние два дня плюс период после последней успешной синхронизации; явные `--start/--end` задают backfill. Для пульса и заряда кольца большие интервалы автоматически разбиваются на части не длиннее 30 дней. Пагинация полная, повторный токен страницы вызывает ошибку. Записи обновляются по ID (временные ряды — по timestamp), без дублей. Страница/коллекция целиком сохраняется транзакцией; сбой не оставляет половину коллекции. Частичные сбои других показателей не теряют успешные данные и дают ненулевой exit code.

Если владелец разрешил только часть категорий, оператор может задать непустой список `sync_endpoints` в его приватном `profile.json`. Обычный `sync` использует этот набор; явные `--endpoint` переопределяют его для проверки доступа. Расширять набор нужно после нового согласия владельца и успешной проверки соответствующих запросов. Известный отказ из-за отсутствующего scope возвращает `permission_missing` и не обновляет действительный refresh token.

`import-archive` переносит прежние каталоги `YYYY-MM-DD/*.json`, проверяя account ID в каждом `personal_info.json` до начала записи. Оригиналы не перемещаются и не удаляются. Если при проверенной смене приложения Oura изменился user ID, оператор может сохранить прежний ID в `historical_account_ids` профиля: этот список действует только для импорта архива, никогда для живого API или входа. Для совместимости старых скриптов `sync --legacy-output /path/to/old-data` дополнительно пишет прежний JSON-envelope и manifest в папку дня; существующий `personal_info.json` сохраняется.

`read` возвращает исходные поля Oura и метаданные источника/владельца/свежести. `sleep` отдельно выделяет основной `long_sleep`, дневной сон и остальные сессии. Продолжительность — секунды, HRV — мс, пульс — уд/мин. `null` не превращается в ноль. `fetched_at` — время скачивания; `latest_day` — день измерений. Отсутствие новых данных не считается доказательством поломки кольца. Сон и готовность зависят от синхронизации приложения Oura.

JSON-вывод имеет `schema_version: 1` для записей. `--quiet` подавляет успешный вывод для cron, ошибки остаются в stderr. Exit codes: 0 успех; 2 аргументы/конфигурация/локальное состояние; 3 доступ/сеть/API; 4 вход ожидает завершения; 5 частичный сбой; 130 прерывание.

## Relay и вебхуки

Oura принимает webhook callback только по HTTPS на порту 443. `public_url` сервера задаёт адрес для вебхуков и OAuth redirect; `relay_url` CLI может быть отдельным доступным адресом управления. При регистрации владельца сервер возвращает публичный callback, который CLI сохраняет для создания, проверки и продления подписок.

Relay нужен только для мобильного OAuth и приёма событий. Отдельный файл конфигурации вне Git, 0600:

```json
{
  "public_url": "https://YOUR-RELAY",
  "registration_key": "GENERATE_A_RANDOM_32_BYTE_SECRET",
  "client_id": "YOUR_APP_ID",
  "client_secret": "YOUR_APP_SECRET",
  "state_dir": "/private/path/relay-state"
}
```

```sh
oura relay serve --config /private/path/relay.json --listen 127.0.0.1 --port 18765
# HTTPS reverse proxy направляет запросы только на этот loopback listener.
oura webhooks enable --endpoint sleep --endpoint daily_readiness
oura webhooks status --json
oura webhooks pull --json
oura webhooks worker --interval 30
oura webhooks maintain --json
oura webhooks disable --json
```

Relay поддерживает ровно одно Oura OAuth-приложение; для другого приложения нужен отдельный relay/config. Конфигурация может быть подготовлена без client ID/secret, но OAuth/webhooks не будут готовы до их заполнения. Публично выставляйте только HTTPS через reverse proxy. Bootstrap/enrollment key передаётся владельцам приватно, а не вместе с кодом. У каждой установки свои poll secrets и случайный идентификатор очереди. Webhook callback общий для приложения; события направляются только в очереди с совпадающим Oura user ID. События незарегистрированных пользователей подтверждаются без хранения.

HMAC проверяется по точным байтам тела и заголовку timestamp, допускается отклонение 5 минут. Подпись, user ID, тип события и объект проверяются до помещения в очередь. Relay хранит только метаданные события — не биометрию. Каждый CLI получает документы собственным Oura-токеном; ACK отправляется после коммита в локальную SQLite. Дубликаты и перезапуски не создают повторных записей; ошибки API оставляют событие для следующей попытки. Delete помечает запись удалённой. Подписки create/update/delete создаются идемпотентно, продлеваются до истечения, чужие callback URL не меняются. `pull` обслуживает подписки раз в час.

Oura не предоставляет webhook-типы для пульса и заряда кольца. Для них и восстановления пропущенных событий оставьте периодический `sync` (например 10:00/21:00). Автоматические сообщения/LLM-анализ не запускаются этим пакетом.

## Hermes

```sh
oura skill install --destination /path/to/hermes/profile/skills
```

Навык поставляется внутри wheel и в `skills/oura-cli/`. Установите CLI в PATH соответствующего агента и задайте его собственный `OURA_CLI_HOME`. Навык не требует рестарта Telegram poller и не включает рассылки. Фактическое обнаружение навыка проверяется штатным списком навыков Hermes.

## Условия Oura

[API/MCP Agreement](https://cloud.ouraring.com/legal/api-agreement), действует с 8 июня 2026, §4(d), ограничивает подачу API-данных в LLM и указывает сервер MCP самой Oura. Этот CLI — детерминированный API-клиент. Сторонняя MCP-обёртка не является официальным MCP и не подтверждает разрешение такого использования. Разрешённый маршрут передачи биометрии в модель оператор выясняет отдельно. Пакет не вызывает модели и не отправляет данные третьим лицам.

Официальные источники: [API V2](https://cloud.ouraring.com/v2/docs), [OAuth2](https://cloud.ouraring.com/docs/authentication), [Webhooks](https://cloud.ouraring.com/v2/docs#tag/Webhook-Subscription-Routes).

## Проверка

```sh
python -m pytest -q
ruff check .
python -m build
```

Интеграционные тесты запускают настоящие CLI-процессы, локальный OAuth/API-сервер и HTTP relay. Все данные синтетические. Они не заменяют ручное согласие реального владельца Oura и получение реального provider webhook; эти этапы фиксируются отдельно в приёмке установки.
