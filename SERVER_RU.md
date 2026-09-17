# 🖥️ Запуск на сервере (краткий гайд)

## Вариант A — systemd (рекомендуется)

```bash
# 1. Клонируем
sudo mkdir -p /opt/kickminer && sudo chown $USER /opt/kickminer
git clone https://github.com/Savo4ek52/Kick_Channel_Points_Miner /opt/kickminer
cd /opt/kickminer

# 2. Окружение
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Конфиг
cp config.example.json config.json
nano config.json   # токены БЕЗ слова Bearer, Telegram enabled=true

# 4. Проверка токенов (не запуская майнер)
.venv/bin/python check_kick_token.py config.json

# 5. systemd-юнит
sudo cp deploy/kickminer.service /etc/systemd/system/
sudo nano /etc/systemd/system/kickminer.service  # поправьте User/пути
sudo systemctl daemon-reload
sudo systemctl enable --now kickminer

# 6. Логи и статус
journalctl -u kickminer -f
systemctl status kickminer
```

Обновление:

```bash
cd /opt/kickminer && git pull && sudo systemctl restart kickminer
```

## Вариант B — Docker

```bash
cp config.example.json config.json
nano config.json
mkdir -p data   # сюда можно вынести логи/статистику (см. ниже)
docker compose up -d --build
docker logs -f kick-miner
```

Healthcheck ходит на `http://127.0.0.1:5000/healthz` (если дашборд
выключен — проверяется живость процесса).

## Полезные опции config.json для сервера

| Ключ | По умолчанию | Что делает |
|---|---|---|
| `Log_file` | `miner.log` | Файл логов с ротацией (10 МБ). `""` — только консоль |
| `Log_retention_days` | `7` | Сколько хранить старые логи |
| `Stats_db` | `miner_stats.db` | SQLite-база истории поинтов (для `/stats` и гейнов в дашборде) |
| `Token_check_hours` | `6` | Как часто сторож проверяет токены. `0` — выкл |
| `Reconnect_cooldown` | `600` | Пауза перед переподключением упавшего WS (сек) |
| `Accounts[].disabled` | `false` | Аккаунт на паузе со старта (`/pause` ставит флаг сам) |

В Docker можно писать логи/статистику в примонтированную папку:

```json
"Log_file": "data/miner.log",
"Stats_db": "data/miner_stats.db"
```

## Telegram-команды (админ = chat_id)

| Команда | Что делает |
|---|---|
| `/status` `/balance` `/accounts` | Мониторинг |
| `/account <alias\|№>` | Карточка аккаунта |
| `/stats [часов]` | Прирост поинтов за период |
| `/errors` | Последние ошибки |
| `/logs [N]` | Последние N строк лога |
| `/addstreamer` `/delstreamer` | Стримеры на лету |
| `/move <alias> <ник> <поз>` | Переставить приоритет |
| `/setlimit <alias> <1-10>` | Лимит фарма |
| `/settoken <alias> <токен>` | Замена токена (сообщение удаляется) |
| `/pause` `/resume` | Пауза аккаунта |
| `/checktokens` | Проверка всех токенов |
| `/restart` | Перезапуск |

## Если что-то пошло не так

1. `python check_kick_token.py config.json` — 90% проблем это протухший токен.
2. `/errors` и `/logs` в Telegram.
3. `journalctl -u kickminer --since "30 min ago"` / `docker logs --tail 200 kick-miner`.
