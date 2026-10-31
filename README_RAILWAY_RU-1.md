# Скубі • Кеш v1.1.11 RC — Railway

## Состав
- `bot.py` — Telegram bot (v1.1.11 RC)
- `database.py` — SQLite layer (v1.1.10 STABLE; не изменён)
- `config.py` — configuration via environment variables
- `requirements.txt` — pinned Python dependencies
- `Dockerfile` — reproducible Python 3.11 deployment
- `test_bot_v111.py` — regression test suite (33 tests)

## Перед публикацией в GitHub
1. Создайте приватный репозиторий.
2. Загрузите содержимое этой папки в корень репозитория.
3. Не загружайте `.env`, токен бота, `scooby.db`, резервные копии или WAL/SHM-файлы.

## Развёртывание на Railway
1. Создайте новый Project → Deploy from GitHub Repo и выберите репозиторий.
2. В настройках сервиса создайте Volume и установите Mount Path: `/data`.
3. В Variables добавьте:
   - `BOT_TOKEN` — токен от BotFather
   - `ADMIN_IDS` — числовой Telegram ID администратора; несколько ID через запятую
   - `DB_NAME` — `/data/scooby.db`
   - `START_BONUS` — `50` (если это соответствует утверждённым настройкам проекта)
   - `REFERRAL_BONUS` — `100`
   - `MAX_BACKUPS` — `3`
4. Используйте один работающий экземпляр/реплику бота: Telegram polling не должен запускаться одновременно в двух экземплярах с одним токеном.
5. Запустите Deploy. Dockerfile запускает `python bot.py`.

## Важно о базе данных
- Volume должен быть подключён к `/data` ДО первого запуска.
- `DB_NAME=/data/scooby.db` обязателен, иначе SQLite окажется в эфемерной файловой системе контейнера.
- Не запускайте новую версию на старой production-БД без отдельной резервной копии и проверки совместимости схемы.
- Не включайте `scooby.db` в GitHub.

## Локальная проверка
```bash
python -m pip install -r requirements.txt
python -m py_compile bot.py config.py database.py test_bot_v111.py
```

Тесты запускаются только в изолированной среде с корректно заданными `BOT_TOKEN` и `ADMIN_IDS`; они не должны использовать production DB.
