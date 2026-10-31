"""
database.py — ядро Скубі • Кеш v1.1.10 RC (STABLE)

Принципы:
1. balance_ledger — единственный источник истины для финансов.
2. users.balance — кэш-проекция, восстанавливается из ledger.
3. Все финансовые операции атомарны (BEGIN IMMEDIATE + rollback).
4. Схема создаётся с нуля (CREATE TABLE IF NOT EXISTS).
5. Будущие изменения — через ensure_column(), без DROP.
6. Бэкап атомарный (tmp + rename), restore с пост-проверкой и авто-откатом.
7. Защита от schema_version=0 на непустой БД, от 1..4 и от >SCHEMA_VERSION.
8. Уникальность operation_id проверяется по фактическому UNIQUE-индексу.

Совместимость: Python 3.10+, SQLite 3.24+, Android Bot Hosting.
"""

import os
import uuid
import hashlib
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import config

logger = logging.getLogger(__name__)

LEDGER_AMOUNT_LIMIT = 1_000_000
OFFER_COST_LIMIT = LEDGER_AMOUNT_LIMIT
MIN_SUPPORTED_SCHEMA = 1


# ============================================================
#  ПУТИ
# ============================================================

def _prepare_db_path() -> str:
    db_path = Path(config.DB_NAME).resolve()
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.critical(f"❌ Не удалось создать папку для БД: {db_path.parent} ({e})")
        raise
    return str(db_path)


DB_PATH = _prepare_db_path()
logger.info(f"📁 DB path (resolved): {DB_PATH}")


# ============================================================
#  ПОДКЛЮЧЕНИЕ
# ============================================================

@contextmanager
def db(immediate: bool = False):
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA busy_timeout=5000;')
        conn.execute('PRAGMA foreign_keys=ON;')
        if immediate:
            conn.execute('BEGIN IMMEDIATE;')
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ============================================================
#  ХЭШИРОВАНИЕ КАТЕГОРИЙ
# ============================================================

def category_key(category: str) -> str:
    try:
        with db() as c:
            rows = c.execute(
                'SELECT DISTINCT category FROM offers WHERE active=1'
            ).fetchall()
    except sqlite3.OperationalError:
        rows = []

    for salt in range(5):
        key = hashlib.sha256(f"{category}:{salt}".encode('utf-8')).hexdigest()[:16]
        if not any(
            hashlib.sha256(f"{r['category']}:{salt}".encode('utf-8')).hexdigest()[:16] == key
            and r['category'] != category
            for r in rows
        ):
            return key
    return hashlib.sha256(category.encode('utf-8')).hexdigest()[:16]


def get_category_by_key(key: str):
    try:
        with db() as c:
            rows = c.execute(
                'SELECT DISTINCT category FROM offers WHERE active=1'
            ).fetchall()
    except sqlite3.OperationalError:
        return None

    for salt in range(5):
        for r in rows:
            if hashlib.sha256(f"{r['category']}:{salt}".encode('utf-8')).hexdigest()[:16] == key:
                return r['category']
    return None


# ============================================================
#  ОЖИДАЕМАЯ СХЕМА
# ============================================================

EXPECTED_TABLES = {
    'users': {'user_id', 'username', 'balance', 'reg_date'},
    'referrals': {'referred_id', 'referrer_id', 'bonus', 'created_at'},
    'balance_ledger': {'id', 'operation_id', 'user_id', 'amount',
                       'reason', 'task_id', 'created_at'},
    'offers': {'id', 'category', 'title', 'description', 'cost',
               'active', 'created_at'},
    'orders': {'id', 'user_id', 'offer_id', 'offer_title', 'cost',
               'status', 'admin_id', 'completed_at',
               'notification_status', 'created_at'},
    'tasks': {'id', 'title', 'description', 'reward', 'task_type',
              'link', 'chat_id', 'is_active', 'created_at'},
    'user_tasks': {'user_id', 'task_id', 'status', 'proof_text',
                   'proof_photo', 'created_at', 'verified_at', 'admin_id'},
}

EXPECTED_INDEXES = {
    'idx_ledger_user': ('balance_ledger', ['user_id'], False),
    'idx_orders_user': ('orders', ['user_id'], False),
    'idx_orders_status': ('orders', ['status'], False),
    'idx_user_tasks_status': ('user_tasks', ['status'], False),
}


def _check_schema_integrity(c) -> list[str]:
    problems = []

    for table, expected_cols in EXPECTED_TABLES.items():
        row = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,)
        ).fetchone()
        if not row:
            problems.append(f"missing_table: {table}")
            continue
        actual = {r['name'] for r in c.execute(f'PRAGMA table_info({table})').fetchall()}
        missing = expected_cols - actual
        if missing:
            problems.append(f"missing_columns: {table}.{sorted(missing)}")

    for idx_name, (table, cols, is_unique) in EXPECTED_INDEXES.items():
        row = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,)
        ).fetchone()
        if not row:
            continue
        row_idx = c.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            (idx_name,)
        ).fetchone()
        if not row_idx:
            problems.append(f"missing_index: {idx_name}")
            continue
        try:
            real_cols = [r['name'] for r in c.execute(f'PRAGMA index_info({idx_name})').fetchall()]
        except sqlite3.Error as e:
            problems.append(f"index_info_failed: {idx_name}: {e}")
            continue
        if real_cols != cols:
            problems.append(f"wrong_index_columns: {idx_name}.{real_cols} != {cols}")
        for r in c.execute(f'PRAGMA index_list({table})').fetchall():
            if r['name'] == idx_name:
                if bool(r['unique']) != bool(is_unique):
                    problems.append(f"wrong_index_uniqueness: {idx_name}")
                break

    row = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='balance_ledger'"
    ).fetchone()
    if row:
        has_unique_op_id = False
        try:
            for idx in c.execute('PRAGMA index_list(balance_ledger)').fetchall():
                if not idx['unique']:
                    continue
                idx_cols = [
                    r['name'] for r in c.execute(
                        f'PRAGMA index_info({idx["name"]})'
                    ).fetchall()
                ]
                if idx_cols == ['operation_id']:
                    has_unique_op_id = True
                    break
        except sqlite3.Error as e:
            problems.append(f"operation_id_unique_check_failed: {e}")
        if not has_unique_op_id:
            problems.append("missing_unique_index: balance_ledger.operation_id")

        try:
            row = c.execute('''
                SELECT COUNT(*) AS cnt FROM (
                    SELECT operation_id FROM balance_ledger
                    GROUP BY operation_id HAVING COUNT(*) > 1
                )
            ''').fetchone()
            if row['cnt'] > 0:
                problems.append(f"duplicate_operation_id: {row['cnt']}")
        except sqlite3.Error as e:
            problems.append(f"duplicate_check_failed: {e}")

    return problems


# ============================================================
#  БЭКАП
# ============================================================

def backup_db() -> str | None:
    if not os.path.exists(DB_PATH):
        return None

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    backup_path = f"{DB_PATH}.backup_{timestamp}"
    tmp_path = f"{backup_path}.tmp"

    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(tmp_path)
    try:
        src.backup(dst)
        check = dst.execute('PRAGMA integrity_check;').fetchone()
        if check[0] != 'ok':
            raise RuntimeError(f"Бэкап повреждён: {check[0]}")
    except Exception:
        try:
            dst.close()
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
        raise
    finally:
        try:
            src.close()
        except Exception:
            pass
        try:
            dst.close()
        except Exception:
            pass

    os.replace(tmp_path, backup_path)

    backup_dir = Path(DB_PATH).parent
    prefix = f"{Path(DB_PATH).name}.backup_"
    try:
        backups = sorted(f for f in os.listdir(backup_dir) if f.startswith(prefix))
        while len(backups) > config.MAX_BACKUPS:
            try:
                os.remove(backup_dir / backups.pop(0))
            except OSError as e:
                logger.warning(f"Не удалось удалить бэкап: {e}")
    except OSError as e:
        logger.warning(f"Ошибка ротации бэкапов: {e}")

    return backup_path


def _remove_sqlite_sidecar_files(db_path: str) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = db_path + suffix
        try:
            if os.path.exists(sidecar):
                os.remove(sidecar)
        except OSError as e:
            logger.warning(f"Не удалось удалить {sidecar}: {e}")


def restore_backup(backup_path: str, *, assume_stopped: bool = True) -> dict:
    if not os.path.exists(backup_path):
        return {'ok': False, 'reason': 'backup_not_found'}

    try:
        conn = sqlite3.connect(backup_path)
    except sqlite3.Error as e:
        return {'ok': False, 'reason': f'backup_open_failed: {e}'}

    try:
        conn.row_factory = sqlite3.Row
        try:
            check = conn.execute('PRAGMA integrity_check;').fetchone()
        except sqlite3.Error as e:
            return {'ok': False, 'reason': f'backup_corrupt: {e}'}
        if check[0] != 'ok':
            return {'ok': False, 'reason': f'backup_corrupt: {check[0]}'}
        try:
            problems = _check_schema_integrity(conn.cursor())
        except Exception as e:
            return {'ok': False, 'reason': f'backup_schema_error: {e}'}
        if problems:
            return {'ok': False, 'reason': f'backup_schema_invalid: {problems}'}
        try:
            fk = conn.execute('PRAGMA foreign_key_check;').fetchall()
        except sqlite3.Error as e:
            return {'ok': False, 'reason': f'backup_fk_check_failed: {e}'}
        if fk:
            return {'ok': False, 'reason': f'backup_fk_violations: {[dict(x) for x in fk]}'}
    finally:
        conn.close()

    pre_restore = None
    if os.path.exists(DB_PATH):
        ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        pre_restore = f"{DB_PATH}.pre_restore_{ts}"
        try:
            src = sqlite3.connect(DB_PATH)
            dst = sqlite3.connect(pre_restore)
            try:
                src.backup(dst)
            finally:
                src.close()
                dst.close()
        except sqlite3.Error as e:
            return {'ok': False, 'reason': f'pre_restore_failed: {e}'}

    ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    tmp_restore = f"{DB_PATH}.restore_{ts}.tmp"
    try:
        src = sqlite3.connect(backup_path)
        dst = sqlite3.connect(tmp_restore)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        if assume_stopped:
            _remove_sqlite_sidecar_files(DB_PATH)
        os.replace(tmp_restore, DB_PATH)
    except Exception as e:
        try:
            if os.path.exists(tmp_restore):
                os.remove(tmp_restore)
        except OSError:
            pass
        return {'ok': False, 'reason': f'restore_failed: {e}',
                'pre_restore': pre_restore}

    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.row_factory = sqlite3.Row
            check = conn.execute('PRAGMA integrity_check;').fetchone()
            if check[0] != 'ok':
                raise RuntimeError(f'integrity_check: {check[0]}')
            problems = _check_schema_integrity(conn.cursor())
            if problems:
                raise RuntimeError(f'schema: {problems}')
            fk = conn.execute('PRAGMA foreign_key_check;').fetchall()
            if fk:
                raise RuntimeError(f'fk: {[dict(x) for x in fk]}')
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"restore: пост-проверка провалена: {e}")
        if pre_restore and os.path.exists(pre_restore):
            try:
                if assume_stopped:
                    _remove_sqlite_sidecar_files(DB_PATH)
                src = sqlite3.connect(pre_restore)
                dst = sqlite3.connect(DB_PATH + ".rollback.tmp")
                try:
                    src.backup(dst)
                finally:
                    src.close()
                    dst.close()
                if assume_stopped:
                    _remove_sqlite_sidecar_files(DB_PATH)
                os.replace(DB_PATH + ".rollback.tmp", DB_PATH)
                logger.info("restore: откат на pre_restore завершён")
            except Exception as e2:
                logger.critical(f"restore: КРИТИЧНО — откат не удался: {e2}")
        return {'ok': False, 'reason': f'post_restore_check_failed: {e}',
                'pre_restore': pre_restore}

    return {'ok': True, 'restored_from': backup_path, 'pre_restore': pre_restore}

# ============================================================
#  ИНИЦИАЛИЗАЦИЯ
# ============================================================

def _ensure_column(c, table: str, col: str, definition: str):
    existing = {r['name'] for r in c.execute(f'PRAGMA table_info({table})').fetchall()}
    if col not in existing:
        c.execute(f'ALTER TABLE {table} ADD COLUMN {col} {definition}')
        logger.info(f"➕ Добавлена колонка {table}.{col}")


def _list_user_tables(c) -> set[str]:
    rows = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r['name'] for r in rows}


def init_db():
    backup_db()

    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        c = conn.cursor()
        c.execute('PRAGMA journal_mode=WAL;')
        c.execute('PRAGMA foreign_keys=ON;')
        c.execute('BEGIN IMMEDIATE;')

        c.execute('CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)')
        row = c.execute('SELECT MAX(version) AS v FROM schema_version').fetchone()
        current_v = row['v'] if row and row['v'] is not None else 0

        user_tables = _list_user_tables(c)
        user_tables_without_version = user_tables - {'schema_version'}

        if current_v > config.SCHEMA_VERSION:
            raise RuntimeError(
                f"БД имеет schema_version={current_v}, "
                f"а код поддерживает только до v{config.SCHEMA_VERSION}. "
                f"Обновите код или восстановите совместимый бэкап."
            )

        if current_v == 0 and user_tables_without_version:
            raise RuntimeError(
                f"БД содержит таблицы ({sorted(user_tables_without_version)}), "
                f"но schema_version=0. Требуется проверенная миграция. "
                f"Автоматический старт запрещён."
            )

        if 0 < current_v < config.SCHEMA_VERSION:
            raise RuntimeError(
                f"БД имеет schema_version={current_v}, но миграции "
                f"с версий 1..{config.SCHEMA_VERSION - 1} не реализованы. "
                f"Поддерживается только свежая БД (v0) или v{config.SCHEMA_VERSION}. "
                f"Восстановите совместимый бэкап или удалите БД для чистого старта."
            )

        c.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY CHECK(user_id > 0),
            username TEXT,
            balance INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0),
            reg_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

        c.execute('''CREATE TABLE IF NOT EXISTS referrals (
            referred_id INTEGER PRIMARY KEY,
            referrer_id INTEGER NOT NULL,
            bonus INTEGER NOT NULL CHECK(bonus >= 0),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(referred_id) REFERENCES users(user_id) ON DELETE CASCADE,
            FOREIGN KEY(referrer_id) REFERENCES users(user_id) ON DELETE RESTRICT)''')

        c.execute(f'''CREATE TABLE IF NOT EXISTS balance_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL CHECK(amount BETWEEN -{LEDGER_AMOUNT_LIMIT} AND {LEDGER_AMOUNT_LIMIT}),
            reason TEXT NOT NULL,
            task_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(user_id))''')

        c.execute(f'''CREATE TABLE IF NOT EXISTS offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            cost INTEGER NOT NULL CHECK(cost >= 0 AND cost <= {OFFER_COST_LIMIT}),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

        c.execute('''CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            offer_id INTEGER,
            offer_title TEXT NOT NULL,
            cost INTEGER NOT NULL CHECK(cost >= 0),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'completed', 'cancelled')),
            admin_id INTEGER,
            completed_at TIMESTAMP,
            notification_status TEXT NOT NULL DEFAULT 'not_sent'
                CHECK(notification_status IN ('not_sent', 'sent', 'failed', 'user_blocked', 'user_deleted')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(user_id))''')

        c.execute('''CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL CHECK(length(title) > 0),
            description TEXT NOT NULL CHECK(length(description) > 0),
            reward INTEGER NOT NULL CHECK(reward > 0 AND reward <= 10000),
            task_type TEXT NOT NULL CHECK(task_type IN ('subscribe', 'manual')),
            link TEXT NOT NULL,
            chat_id TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

        c.execute('''CREATE TABLE IF NOT EXISTS user_tasks (
            user_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'approved', 'rejected')),
            proof_text TEXT,
            proof_photo TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            verified_at TIMESTAMP,
            admin_id INTEGER,
            PRIMARY KEY (user_id, task_id),
            FOREIGN KEY(user_id) REFERENCES users(user_id),
            FOREIGN KEY(task_id) REFERENCES tasks(id))''')

        c.execute('CREATE INDEX IF NOT EXISTS idx_ledger_user ON balance_ledger(user_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_user_tasks_status ON user_tasks(status)')

        problems = _check_schema_integrity(c)
        if problems:
            raise RuntimeError(f"Схема не соответствует ожиданиям: {problems}")

        if current_v < config.SCHEMA_VERSION:
            c.execute('INSERT INTO schema_version (version) VALUES (?)',
                      (config.SCHEMA_VERSION,))
            logger.info(f"📊 Схема: v{current_v} → v{config.SCHEMA_VERSION}")

        conn.commit()
        logger.info("✅ init_db завершён.")

    except Exception as e:
        conn.rollback()
        logger.critical(f"❌ init_db провален: {e}")
        raise
    finally:
        conn.close()


# ============================================================
#  АУДИТ ЦЕЛОСТНОСТИ
# ============================================================

def audit_integrity() -> dict:
    report = {'ok': True, 'issues': []}

    def add_issue(**kw):
        report['ok'] = False
        report['issues'].append(kw)

    with db() as c:
        for r in c.execute('''
            SELECT u.user_id, u.balance, COALESCE(SUM(b.amount), 0) AS ledger_sum
            FROM users u
            LEFT JOIN balance_ledger b ON u.user_id = b.user_id
            GROUP BY u.user_id
        ''').fetchall():
            if r['balance'] != r['ledger_sum']:
                add_issue(type='balance_mismatch', user_id=r['user_id'],
                          ledger=r['ledger_sum'], balance=r['balance'])

        for d in c.execute('''SELECT operation_id, COUNT(*) AS cnt
                              FROM balance_ledger
                              GROUP BY operation_id HAVING cnt > 1''').fetchall():
            add_issue(type='duplicate_op', operation_id=d['operation_id'])

        for n in c.execute('SELECT user_id, balance FROM users WHERE balance < 0').fetchall():
            add_issue(type='negative_balance', user_id=n['user_id'])

        for fk in c.execute('PRAGMA foreign_key_check;').fetchall():
            add_issue(type='fk_violation', table=fk[0], rowid=fk[1], parent=fk[2])

        for o in c.execute('''
            SELECT r.referred_id, r.referrer_id FROM referrals r
            LEFT JOIN users u ON r.referrer_id = u.user_id
            WHERE u.user_id IS NULL
        ''').fetchall():
            add_issue(type='orphan_referral', referred_id=o['referred_id'],
                      missing_referrer=o['referrer_id'])

        for o in c.execute('''
            SELECT r.referred_id FROM referrals r
            LEFT JOIN users u ON r.referred_id = u.user_id
            WHERE u.user_id IS NULL
        ''').fetchall():
            add_issue(type='orphan_referral_referred', referred_id=o['referred_id'])

        for o in c.execute('''
            SELECT b.user_id, COUNT(*) AS cnt FROM balance_ledger b
            LEFT JOIN users u ON b.user_id = u.user_id
            WHERE u.user_id IS NULL
            GROUP BY b.user_id
        ''').fetchall():
            add_issue(type='orphan_ledger', user_id=o['user_id'], count=o['cnt'])

        for o in c.execute('''
            SELECT o.user_id, COUNT(*) AS cnt FROM orders o
            LEFT JOIN users u ON o.user_id = u.user_id
            WHERE u.user_id IS NULL
            GROUP BY o.user_id
        ''').fetchall():
            add_issue(type='orphan_order', user_id=o['user_id'], count=o['cnt'])

        for o in c.execute('''
            SELECT ut.user_id, ut.task_id FROM user_tasks ut
            LEFT JOIN users u ON ut.user_id = u.user_id
            LEFT JOIN tasks t ON ut.task_id = t.id
            WHERE u.user_id IS NULL OR t.id IS NULL
        ''').fetchall():
            add_issue(type='orphan_user_task',
                      user_id=o['user_id'], task_id=o['task_id'])

    return report


def reconcile_balance(user_id: int) -> dict:
    with db(immediate=True) as c:
        result = c.execute(
            'SELECT COALESCE(SUM(amount), 0) AS total FROM balance_ledger WHERE user_id=?',
            (user_id,)
        ).fetchone()
        ledger_balance = result['total']

        user = c.execute('SELECT balance FROM users WHERE user_id=?',
                         (user_id,)).fetchone()
        if not user:
            return {'ok': False, 'reason': 'user_not_found'}

        if ledger_balance < 0:
            return {'ok': False, 'reason': 'negative_ledger',
                    'ledger_balance': ledger_balance,
                    'user_balance': user['balance']}

        diff = ledger_balance - user['balance']
        if diff != 0:
            c.execute('UPDATE users SET balance=? WHERE user_id=?',
                      (ledger_balance, user_id))
            logger.info(f"reconcile: {user_id} {user['balance']} → {ledger_balance} (diff={diff})")

        return {'ok': True, 'balance': ledger_balance, 'correction': diff}


# ============================================================
#  ФИНАНСОВЫЕ ОПЕРАЦИИ
# ============================================================

def get_user(user_id: int):
    with db() as c:
        return c.execute('SELECT * FROM users WHERE user_id=?', (user_id,)).fetchone()


def _add_points_in_tx(c, user_id: int, amount: int, reason: str,
                     task_id: int | None = None) -> None:
    if abs(amount) > LEDGER_AMOUNT_LIMIT:
        raise ValueError(f"amount {amount} превышает лимит {LEDGER_AMOUNT_LIMIT}")
    cursor = c.execute(
        'UPDATE users SET balance = balance + ? WHERE user_id = ?',
        (amount, user_id)
    )
    if cursor.rowcount == 0:
        raise sqlite3.IntegrityError(f"Пользователь {user_id} не найден")
    c.execute(
        '''INSERT INTO balance_ledger (operation_id, user_id, amount, reason, task_id)
           VALUES (?, ?, ?, ?, ?)''',
        (str(uuid.uuid4()), user_id, amount, reason[:200], task_id)
    )


def register_user(user_id: int, username: str, ref_code: str | None = None) -> tuple:
    if user_id <= 0:
        return False, None

    with db(immediate=True) as c:
        try:
            cursor = c.execute(
                'INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)',
                (user_id, username)
            )
        except sqlite3.IntegrityError as e:
            logger.warning(f"register_user integrity: {e}")
            raise
        if cursor.rowcount == 0:
            return False, None

        _add_points_in_tx(c, user_id, config.START_BONUS, 'start_bonus')

        referrer_id = None
        if ref_code and ref_code.isdigit() and int(ref_code) != user_id:
            ref_id = int(ref_code)
            if c.execute('SELECT 1 FROM users WHERE user_id=?', (ref_id,)).fetchone():
                c.execute(
                    'INSERT INTO referrals (referred_id, referrer_id, bonus) VALUES (?, ?, ?)',
                    (user_id, ref_id, config.REFERRAL_BONUS)
                )
                _add_points_in_tx(c, ref_id, config.REFERRAL_BONUS,
                                  f'ref_bonus_from_{user_id}')
                referrer_id = ref_id

        return True, referrer_id


# ============================================================
#  МАГАЗИН
# ============================================================

def add_offer(category: str, title: str, description: str, cost: int):
    if cost < 0 or cost > OFFER_COST_LIMIT:
        raise ValueError(f"cost {cost} вне диапазона [0, {OFFER_COST_LIMIT}]")
    with db() as c:
        c.execute(
            'INSERT INTO offers (category, title, description, cost) VALUES (?, ?, ?, ?)',
            (category, title, description, cost)
        )


def get_categories():
    with db() as c:
        return c.execute(
            'SELECT DISTINCT category FROM offers WHERE active=1 ORDER BY category'
        ).fetchall()


def get_offers_by_category(category: str):
    with db() as c:
        return c.execute(
            '''SELECT id, title, cost FROM offers
               WHERE category=? AND active=1 ORDER BY id DESC LIMIT 20''',
            (category,)
        ).fetchall()


def get_offer(offer_id: int):
    with db() as c:
        return c.execute(
            'SELECT * FROM offers WHERE id=? AND active=1', (offer_id,)
        ).fetchone()


def get_user_orders(user_id: int):
    with db() as c:
        return c.execute(
            'SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10',
            (user_id,)
        ).fetchall()


def create_order(user_id: int, offer_id: int) -> tuple:
    with db(immediate=True) as c:
        user = c.execute('SELECT balance FROM users WHERE user_id=?',
                         (user_id,)).fetchone()
        if not user:
            return False, "Пользователь не найден."

        offer = c.execute('SELECT * FROM offers WHERE id=? AND active=1',
                         (offer_id,)).fetchone()
        if not offer:
            return False, "Товар недоступен."

        cost = offer['cost']
        if cost > LEDGER_AMOUNT_LIMIT:
            return False, "Стоимость товара превышает лимит ledger."
        if user['balance'] < cost:
            return False, "Недостаточно баллов."

        cursor = c.execute(
            'UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?',
            (cost, user_id, cost)
        )
        if cursor.rowcount == 0:
            return False, "Ошибка списания."

        cur_ins = c.execute(
            'INSERT INTO orders (user_id, offer_id, offer_title, cost) VALUES (?, ?, ?, ?)',
            (user_id, offer_id, offer['title'], cost)
        )
        order_id = cur_ins.lastrowid

        c.execute(
            '''INSERT INTO balance_ledger (operation_id, user_id, amount, reason)
               VALUES (?, ?, ?, ?)''',
            (str(uuid.uuid4()), user_id, -cost, f'order_{order_id}')
        )

        return True, f"Заказ №{order_id} оформлен!"


def get_pending_orders():
    with db() as c:
        return c.execute(
            'SELECT * FROM orders WHERE status = "pending" ORDER BY id ASC'
        ).fetchall()


def get_order(order_id: int):
    with db() as c:
        return c.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()


def complete_order(order_id: int, admin_id: int) -> bool:
    with db() as c:
        cursor = c.execute(
            '''UPDATE orders
               SET status = "completed", admin_id = ?, completed_at = CURRENT_TIMESTAMP
               WHERE id = ? AND status = "pending"''',
            (admin_id, order_id)
        )
        return cursor.rowcount > 0


def update_notification_status(order_id: int, status: str):
    with db() as c:
        c.execute('UPDATE orders SET notification_status = ? WHERE id = ?',
                  (status, order_id))


# ============================================================
#  ЗАДАНИЯ
# ============================================================

def add_task(title: str, description: str, reward: int,
             task_type: str, link: str, chat_id: str | None = None):
    if not isinstance(task_type, str) or task_type not in ('subscribe', 'manual'):
        raise ValueError("task_type должен быть 'subscribe' или 'manual'")

    if not isinstance(reward, int) or isinstance(reward, bool):
        raise ValueError(f"reward должен быть целым числом, получено {type(reward).__name__}")

    if task_type == 'subscribe':
        reward = 100

    if reward <= 0 or reward > 10000:
        raise ValueError(f"reward {reward} вне диапазона [1, 10000]")

    if not isinstance(title, str) or not title.strip():
        raise ValueError("title не может быть пустым")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description не может быть пустым")
    if not isinstance(link, str) or not link.strip():
        raise ValueError("link не может быть пустым")

    title = title.strip()[:200]
    description = description.strip()[:1000]
    link = link.strip()[:500]
    if chat_id is not None:
        chat_id = str(chat_id).strip()[:100] or None

    with db() as c:
        c.execute(
            '''INSERT INTO tasks (title, description, reward, task_type, link, chat_id)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (title, description, reward, task_type, link, chat_id)
        )


def get_active_tasks():
    with db() as c:
        return c.execute(
            'SELECT * FROM tasks WHERE is_active=1 ORDER BY id DESC LIMIT 20'
        ).fetchall()


def get_task(task_id: int):
    with db() as c:
        return c.execute(
            'SELECT * FROM tasks WHERE id=? AND is_active=1', (task_id,)
        ).fetchone()


def get_user_task(user_id: int, task_id: int):
    with db() as c:
        return c.execute(
            'SELECT * FROM user_tasks WHERE user_id=? AND task_id=?',
            (user_id, task_id)
        ).fetchone()


def get_pending_user_tasks():
    with db() as c:
        return c.execute('''
            SELECT ut.user_id, ut.task_id, ut.created_at, ut.proof_text,
                   ut.proof_photo, t.title, t.reward, t.description
            FROM user_tasks ut
            JOIN tasks t ON ut.task_id = t.id
            WHERE ut.status = 'pending' AND t.task_type = 'manual'
            ORDER BY ut.created_at ASC
        ''').fetchall()


def claim_auto_task(user_id: int, task_id: int) -> tuple:
    """
    Атомарно: заявка + начисление.
    Любая ошибка (включая IntegrityError) → raise → ROLLBACK всей транзакции.

    ВАЖНО: эта функция НЕ проверяет подписку пользователя в Telegram.
    Проверка get_chat_member должна выполняться в bot.py ДО вызова.
    Иначе пользователь получит награду за задание, которое не выполнил.
    Здесь только атомарность "одно выполнение → одна награда".
    """
    with db(immediate=True) as c:
        task = c.execute(
            '''SELECT reward FROM tasks
               WHERE id=? AND is_active=1 AND task_type="subscribe"''',
            (task_id,)
        ).fetchone()
        if not task:
            return False, 'not_found'

        existing = c.execute(
            'SELECT status FROM user_tasks WHERE user_id=? AND task_id=?',
            (user_id, task_id)
        ).fetchone()
        if existing:
            return False, 'already_done'

        c.execute(
            '''INSERT INTO user_tasks (user_id, task_id, status, verified_at)
               VALUES (?, ?, "approved", CURRENT_TIMESTAMP)''',
            (user_id, task_id)
        )
        _add_points_in_tx(c, user_id, task['reward'],
                          f'task_reward_{task_id}', task_id)
        return True, 'ok'


def submit_manual_task(user_id: int, task_id: int,
                       proof_text: str = "",
                       proof_photo: str | None = None) -> tuple:
    with db(immediate=True) as c:
        task = c.execute(
            '''SELECT 1 FROM tasks
               WHERE id=? AND is_active=1 AND task_type="manual"''',
            (task_id,)
        ).fetchone()
        if not task:
            return False, 'not_found', "Задание не найдено или неактивно."

        existing = c.execute(
            'SELECT status FROM user_tasks WHERE user_id=? AND task_id=?',
            (user_id, task_id)
        ).fetchone()

        if existing:
            if existing['status'] == 'pending':
                return False, 'pending', "Заявка уже на проверке."
            if existing['status'] == 'approved':
                return False, 'approved', "Задание уже выполнено."

        cursor = c.execute(
            '''INSERT INTO user_tasks (user_id, task_id, status, proof_text, proof_photo)
               VALUES (?, ?, "pending", ?, ?)
               ON CONFLICT(user_id, task_id) DO UPDATE SET
                   status="pending",
                   proof_text=excluded.proof_text,
                   proof_photo=excluded.proof_photo,
                   created_at=CURRENT_TIMESTAMP,
                   verified_at=NULL,
                   admin_id=NULL
               WHERE user_tasks.status="rejected"''',
            (user_id, task_id,
             proof_text[:500] if proof_text else None,
             proof_photo)
        )
        if cursor.rowcount == 0:
            row = c.execute(
                'SELECT status FROM user_tasks WHERE user_id=? AND task_id=?',
                (user_id, task_id)
            ).fetchone()
            if row:
                return False, row['status'], f"Заявка в статусе: {row['status']}"
            return False, 'error', "Не удалось создать заявку."

        return True, 'ok', "Заявка отправлена."


def approve_manual_task(user_id: int, task_id: int, admin_id: int) -> bool:
    with db(immediate=True) as c:
        task = c.execute(
            '''SELECT reward FROM tasks
               WHERE id=? AND is_active=1 AND task_type="manual"''',
            (task_id,)
        ).fetchone()
        if not task:
            logger.warning(f"approve: задание {task_id} не найдено/неактивно")
            return False

        cursor = c.execute(
            '''UPDATE user_tasks
               SET status="approved", verified_at=CURRENT_TIMESTAMP, admin_id=?
               WHERE user_id=? AND task_id=? AND status="pending"''',
            (admin_id, user_id, task_id)
        )
        if cursor.rowcount == 0:
            logger.info(f"approve: заявка {user_id}/{task_id} уже обработана")
            return False

        _add_points_in_tx(c, user_id, task['reward'],
                          f'task_reward_{task_id}', task_id)
        return True


def reject_manual_task(user_id: int, task_id: int, admin_id: int) -> bool:
    with db(immediate=True) as c:
        cursor = c.execute(
            '''UPDATE user_tasks
               SET status="rejected", verified_at=CURRENT_TIMESTAMP, admin_id=?
               WHERE user_id=? AND task_id=? AND status="pending"''',
            (admin_id, user_id, task_id)
        )
        return cursor.rowcount > 0
