Скубі • Кеш 🚐

Telegram referral and rewards bot.

Project status

New development repository.

Architecture

- "bot.py" — Telegram bot handlers and interface.
- "database.py" — database layer and migrations.
- "config.py" — application configuration.
- "requirements.txt" — Python dependencies.

Development principles

- No hardcoded secrets or tokens.
- Explicit database migrations.
- Transaction-safe balance operations.
- Idempotent financial operations.
- Logging and error handling.
- Automated tests before release.

Release policy

No version is considered stable until its source code, database migrations, tests and SHA-256 checksums have been independently verified.

Security

Never commit ".env", Telegram tokens, passwords, private keys or database backups containing user data.
