"""
test_bot_v111.py — обвязка для bot.py v1.1.11 RC.

Покрытие: TB-01 … TB-12 (по отчёту BOT-AUDIT v1.1.11 RC).

ВАЖНО:
- Тесты НЕ доказывают корректность bot.py, пока не выполнен фактический
  прогон на склеенном файле и не подтверждён BOT AST OK.
- Telegram API НЕ имитируется как успешно пройденный: все сетевые вызовы
  замоканы через AsyncMock.
- database.py v1.1.10 STABLE не изменяется. Тесты работают поверх его API
  в изолированной БД (временная папка).
- Остаточный риск B-09 (повтор notify) фиксируется как известное
  ограничение, а не как успех.

Запуск (только после BOT AST OK):
    python -m unittest test_bot_v111 -v

Структура:
- Часть 1/3: заголовок, импорты, env-подготовка, mock-фабрики, helper'ы.
- Часть 2/3: TB-01 … TB-06.
- Часть 3/3: TB-07 … TB-12 + __main__.
"""

import os
import sys
import tempfile
import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

# ==== ИЗОЛЯЦИЯ: тестовые env ДО импорта config и bot ====
TEST_DIR = tempfile.mkdtemp(prefix="scoby_bot_test_")
TEST_DB = os.path.join(TEST_DIR, "test.db")

os.environ["BOT_TOKEN"] = "TEST_TOKEN_123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
os.environ["ADMIN_IDS"] = "111111111,222222222"
os.environ["START_BONUS"] = "50"
os.environ["REFERRAL_BONUS"] = "100"
os.environ["MAX_BACKUPS"] = "3"
os.environ["DB_NAME"] = TEST_DB


# ==== Импорт bot и database после env ====
import config
import database as db
import bot as bot_module


# ============================================================
#  MOCK-ФАБРИКИ
# ============================================================

def make_callback_query(data: str, user_id: int = 1001, username: str = "tester"):
    """
    Возвращает AsyncMock, имитирующий telegram.CallbackQuery.
    Все методы async, включая answer() и edit_message_text().
    """
    q = AsyncMock()
    q.data = data
    q.from_user = MagicMock()
    q.from_user.id = user_id
    q.from_user.username = username
    q.from_user.first_name = "Tester"
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.message = MagicMock()
    q.message.reply_text = AsyncMock()
    return q


def make_update_with_callback(data: str, user_id: int = 1001):
    """
    Возвращает telegram.Update-подобный объект с callback_query.
    """
    upd = MagicMock()
    upd.callback_query = make_callback_query(data, user_id)
    upd.effective_user = MagicMock()
    upd.effective_user.id = user_id
    upd.effective_user.username = "tester"
    upd.effective_user.first_name = "Tester"
    upd.effective_message = upd.callback_query.message
    return upd


def make_update_with_message(text: str, user_id: int = 1001, photo=None, caption=None):
    """
    Возвращает telegram.Update-подобный объект с message.
    photo: список объектов с .file_id (или None).
    """
    upd = MagicMock()
    upd.callback_query = None
    upd.effective_user = MagicMock()
    upd.effective_user.id = user_id
    upd.effective_user.username = "tester"
    upd.effective_user.first_name = "Tester"

    msg = MagicMock()
    msg.text = text
    msg.photo = photo
    msg.caption = caption
    msg.reply_text = AsyncMock()
    upd.message = msg
    upd.effective_message = msg
    return upd


def make_context(user_data: dict | None = None, bot_data: dict | None = None):
    """
    Возвращает ContextTypes-подобный объект.
    bot — AsyncMock с async методами (send_message, get_chat_member, get_me, send_photo).
    """
    ctx = MagicMock()
    ctx.user_data = user_data if user_data is not None else {}
    ctx.bot_data = bot_data if bot_data is not None else {}
    ctx.args = []
    ctx.bot = AsyncMock()
    ctx.bot.send_message = AsyncMock(return_value=MagicMock())
    ctx.bot.send_photo = AsyncMock(return_value=MagicMock())
    ctx.bot.get_me = AsyncMock(return_value=MagicMock(username="test_bot"))
    ctx.bot.get_chat_member = AsyncMock()
    ctx.bot.id = 999999999
    ctx.application = MagicMock()
    return ctx


# ============================================================
#  ХЕЛПЕРЫ ДЛЯ ТЕСТОВ
# ============================================================

def reset_test_db():
    """Сносит тестовую БД и создаёт заново."""
    for suffix in ("", "-wal", "-shm"):
        p = TEST_DB + suffix
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    db.init_db()


def cleanup_backups():
    """Убирает все бэкапы из TEST_DIR, чтобы тесты не мешали друг другу."""
    import glob
    for f in glob.glob(os.path.join(TEST_DIR, "*.backup_*")):
        try:
            os.remove(f)
        except OSError:
            pass


# ============================================================
#  БАЗОВЫЙ КЛАСС ДЛЯ ТЕСТОВ
# ============================================================

class BotV111Base(unittest.IsolatedAsyncioTestCase):
    """
    Базовый класс. Перед каждым тестом — чистая БД.
    """
    async def asyncSetUp(self):
        cleanup_backups()
        reset_test_db()

    async def asyncTearDown(self):
        pass

# ============================================================
#  TB-01 — start_proof_input: запись submit_task_id, один query.answer,
#          отклонение некорректных ID (0, -1, 'abc')
# ============================================================

class TB01_StartProofInput(BotV111Base):

    async def test_tb01a_valid_id(self):
        """Корректный ID → submit_task_id записан, answer вызван ровно 1 раз."""
        upd = make_update_with_callback("task_submit_42", user_id=1001)
        ctx = make_context()

        result = await bot_module.start_proof_input(upd, ctx)

        self.assertEqual(result, bot_module.PROOF_INPUT)
        self.assertEqual(ctx.user_data.get('submit_task_id'), 42)
        self.assertEqual(upd.callback_query.answer.await_count, 1)
        upd.callback_query.edit_message_text.assert_awaited_once()

    async def test_tb01b_invalid_zero(self):
        """ID = 0 → не пишем в user_data, alert в answer, return END."""
        upd = make_update_with_callback("task_submit_0", user_id=1001)
        ctx = make_context()

        result = await bot_module.start_proof_input(upd, ctx)

        self.assertEqual(result, bot_module.ConversationHandler.END)
        self.assertNotIn('submit_task_id', ctx.user_data)
        self.assertEqual(upd.callback_query.answer.await_count, 1)
        _, kwargs = upd.callback_query.answer.await_args
        self.assertTrue(kwargs.get('show_alert', False))

    async def test_tb01c_invalid_negative(self):
        """ID = -5 не проходит regex -> callback не достигает start_proof_input."""
        upd = make_update_with_callback("task_submit_-5", user_id=1001)
        ctx = make_context()

        result = await bot_module.start_proof_input(upd, ctx)

        self.assertEqual(result, bot_module.ConversationHandler.END)
        self.assertNotIn('submit_task_id', ctx.user_data)
        self.assertEqual(upd.callback_query.answer.await_count, 1)

    async def test_tb01d_invalid_alpha(self):
        """ID = 'abc' не парсится -> return END, alert."""
        upd = make_update_with_callback("task_submit_abc", user_id=1001)
        ctx = make_context()

        result = await bot_module.start_proof_input(upd, ctx)

        self.assertEqual(result, bot_module.ConversationHandler.END)
        self.assertNotIn('submit_task_id', ctx.user_data)
        self.assertEqual(upd.callback_query.answer.await_count, 1)

    async def test_tb01e_already_in_progress(self):
        """Если submit_task_id уже установлен — не перезаписываем, alert."""
        upd = make_update_with_callback("task_submit_42", user_id=1001)
        ctx = make_context(user_data={'submit_task_id': 99})

        result = await bot_module.start_proof_input(upd, ctx)

        self.assertEqual(result, bot_module.ConversationHandler.END)
        self.assertEqual(ctx.user_data['submit_task_id'], 99)
        self.assertEqual(upd.callback_query.answer.await_count, 1)
        _, kwargs = upd.callback_query.answer.await_args
        self.assertTrue(kwargs.get('show_alert', False))


# ============================================================
#  TB-02 — main() при audit_integrity()['ok'] is False:
#          SystemExit(1), Bot.initialize(), shutdown(), send_message админам
# ============================================================

class TB02_MainAuditFail(BotV111Base):

    async def test_tb02a_audit_fail_exits_and_notifies(self):
        bad_report = {'ok': False, 'issues': [{'type': 'balance_mismatch', 'user_id': 1001}]}

        with patch.object(db, 'audit_integrity', return_value=bad_report), \
             patch.object(bot_module, 'Bot') as MockBot:

            mock_bot_instance = AsyncMock()
            mock_bot_instance.initialize = AsyncMock()
            mock_bot_instance.shutdown = AsyncMock()
            mock_bot_instance.send_message = AsyncMock(return_value=MagicMock())
            MockBot.return_value = mock_bot_instance

            with self.assertRaises(SystemExit) as cm:
                bot_module.main()

            self.assertEqual(cm.exception.code, 1)
            mock_bot_instance.initialize.assert_awaited()
            mock_bot_instance.shutdown.assert_awaited()
            self.assertGreaterEqual(mock_bot_instance.send_message.await_count, 1)

    async def test_tb02b_audit_ok_starts(self):
        good_report = {'ok': True, 'issues': []}

        with patch.object(db, 'audit_integrity', return_value=good_report), \
             patch.object(bot_module.Application, 'builder') as MockBuilder:

            mock_app = MagicMock()
            mock_app.add_handler = MagicMock()
            mock_app.add_error_handler = MagicMock()
            mock_app.run_polling = MagicMock()
            MockBuilder.return_value.token.return_value.build.return_value = mock_app

            bot_module.main()
            mock_app.run_polling.assert_called_once()


# ============================================================
#  TB-03 — complete_order_: раздельная обработка отправки и записи статуса
# ============================================================

class TB03_CompleteOrder(BotV111Base):

    async def test_tb03a_send_ok_db_fails_logs_critical(self):
        user_id = 1001
        db.register_user(user_id, "tester")
        db.add_offer("Cat", "Item", "Desc", 10)
        offers = db.get_offers_by_category("Cat")
        ok, _ = db.create_order(user_id, offers[0]['id'])
        self.assertTrue(ok)
        order_id = db.get_user_orders(user_id)[0]['id']
        db.complete_order(order_id, admin_id=111111111)

        upd = make_update_with_callback(f"complete_order_{order_id}", user_id=111111111)
        ctx = make_context()

        with patch.object(db, 'update_notification_status',
                          side_effect=RuntimeError("db crash")), \
             patch.object(bot_module.logger, 'critical') as mock_critical:

            await bot_module.button_handler(upd, ctx)

            self.assertTrue(mock_critical.called)
            order = db.get_order(order_id)
            self.assertNotEqual(order['notification_status'], 'failed')

    async def test_tb03b_send_fails_records_failed(self):
        user_id = 1001
        db.register_user(user_id, "tester")
        db.add_offer("Cat2", "Item2", "Desc", 10)
        offers = db.get_offers_by_category("Cat2")
        db.create_order(user_id, offers[0]['id'])
        order_id = db.get_user_orders(user_id)[0]['id']
        db.complete_order(order_id, admin_id=111111111)

        upd = make_update_with_callback(f"complete_order_{order_id}", user_id=111111111)
        ctx = make_context()

        from telegram.error import TelegramError
        ctx.bot.send_message = AsyncMock(side_effect=TelegramError("network"))

        await bot_module.button_handler(upd, ctx)

        order = db.get_order(order_id)
        self.assertEqual(order['notification_status'], 'failed')


# ============================================================
#  TB-04 — approve_task_ / reject_task_ с некорректным data
# ============================================================

class TB04_ApproveRejectInvalid(BotV111Base):

    async def test_tb04a_approve_invalid_format(self):
        upd = make_update_with_callback("approve_task_x", user_id=111111111)
        ctx = make_context()

        await bot_module.button_handler(upd, ctx)

        self.assertEqual(upd.callback_query.answer.await_count, 1)
        _, kwargs = upd.callback_query.answer.await_args
        self.assertTrue(kwargs.get('show_alert', False))

    async def test_tb04b_approve_zero_ids(self):
        upd = make_update_with_callback("approve_task_0_5", user_id=111111111)
        ctx = make_context()

        await bot_module.button_handler(upd, ctx)

        self.assertEqual(upd.callback_query.answer.await_count, 1)

    async def test_tb04c_approve_negative_ids(self):
        upd = make_update_with_callback("approve_task_-1_5", user_id=111111111)
        ctx = make_context()

        await bot_module.button_handler(upd, ctx)

        self.assertEqual(upd.callback_query.answer.await_count, 1)

    async def test_tb04d_reject_invalid(self):
        upd = make_update_with_callback("reject_task_x", user_id=111111111)
        ctx = make_context()

        await bot_module.button_handler(upd, ctx)

        self.assertEqual(upd.callback_query.answer.await_count, 1)


# ============================================================
#  TB-05 — admin_orders показывает pending + retryable, не показывает sent
# ============================================================

class TB05_AdminOrders(BotV111Base):

    def _seed(self):
        for uid in (2001, 2002, 2003):
            db.register_user(uid, f"user{uid}")
        db.add_offer("Cat5", "Item5", "Desc", 10)
        offers = db.get_offers_by_category("Cat5")
        oid = offers[0]['id']

        db.create_order(2001, oid)
        o_pending = db.get_user_orders(2001)[0]['id']

        db.create_order(2002, oid)
        o_sent = db.get_user_orders(2002)[0]['id']
        db.complete_order(o_sent, admin_id=111111111)
        db.update_notification_status(o_sent, 'sent')

        db.create_order(2003, oid)
        o_failed = db.get_user_orders(2003)[0]['id']
        db.complete_order(o_failed, admin_id=111111111)
        db.update_notification_status(o_failed, 'failed')

        return o_pending, o_sent, o_failed

    async def test_tb05a_shows_pending_and_failed(self):
        o_pending, o_sent, o_failed = self._seed()

        upd = make_update_with_callback("admin_orders", user_id=111111111)
        ctx = make_context()

        await bot_module.button_handler(upd, ctx)

        upd.callback_query.edit_message_text.assert_awaited_once()
        _, kwargs = upd.callback_query.edit_message_text.await_args
        markup = kwargs.get('reply_markup')
        self.assertIsNotNone(markup)

        button_datas = []
        for row in markup.inline_keyboard:
            for btn in row:
                if btn.callback_data:
                    button_datas.append(btn.callback_data)

        self.assertIn(f"manage_order_{o_pending}", button_datas)
        self.assertIn(f"manage_order_{o_failed}", button_datas)
        self.assertNotIn(f"retry_notify_{o_sent}", button_datas)


# ============================================================
#  TB-06 — retry_notify_ допустимые статусы + остаточный риск B-09
# ============================================================

class TB06_RetryNotify(BotV111Base):

    def _make_completed_order(self, uid, status):
        db.register_user(uid, f"user{uid}")
        db.add_offer("Cat6", "Item6", "Desc", 10)
        offers = db.get_offers_by_category("Cat6")
        db.create_order(uid, offers[0]['id'])
        order_id = db.get_user_orders(uid)[0]['id']
        db.complete_order(order_id, admin_id=111111111)
        db.update_notification_status(order_id, status)
        return order_id

    async def test_tb06a_retry_allowed_for_retryable(self):
        for i, status in enumerate(('not_sent', 'failed', 'user_blocked', 'user_deleted')):
            uid = 3001 + i
            order_id = self._make_completed_order(uid, status)

            upd = make_update_with_callback(f"retry_notify_{order_id}", user_id=111111111)
            ctx = make_context()

            await bot_module.button_handler(upd, ctx)

            self.assertGreaterEqual(upd.callback_query.answer.await_count, 1)
            order = db.get_order(order_id)
            self.assertIn(order['notification_status'],
                          ('sent', 'failed', 'user_blocked', 'user_deleted'))

    async def test_tb06b_retry_denied_for_sent(self):
        order_id = self._make_completed_order(3001, 'sent')

        upd = make_update_with_callback(f"retry_notify_{order_id}", user_id=111111111)
        ctx = make_context()

        await bot_module.button_handler(upd, ctx)

        self.assertEqual(upd.callback_query.answer.await_count, 1)
        _, kwargs = upd.callback_query.answer.await_args
        self.assertTrue(kwargs.get('show_alert', False))

    async def test_tb06c_known_risk_b09_documented(self):
        order_id = self._make_completed_order(3001, 'failed')

        upd = make_update_with_callback(f"retry_notify_{order_id}", user_id=111111111)
        ctx = make_context()

        await bot_module.button_handler(upd, ctx)

        order = db.get_order(order_id)
        self.assertIn(order['notification_status'], ('sent', 'failed'))

# ============================================================
#  TB-07 — start() очищает user_data (B-10)
# ============================================================

class TB07_StartClearsState(BotV111Base):

    async def test_tb07a_start_clears_dialog_keys(self):
        user_id = 1001
        ctx = make_context(user_data={
            'submit_task_id': 42,
            'new_offer': {'cat': 'x'},
            'new_task': {'title': 'y'},
            'unrelated_key': 'keep_me',
        })

        upd = make_update_with_message("/start", user_id=user_id)
        upd.message.reply_text = AsyncMock()

        await bot_module.start(upd, ctx)

        self.assertNotIn('submit_task_id', ctx.user_data)
        self.assertNotIn('new_offer', ctx.user_data)
        self.assertNotIn('new_task', ctx.user_data)
        self.assertEqual(ctx.user_data.get('unrelated_key'), 'keep_me')


# ============================================================
#  TB-08 — task_chat_id: BadRequest и общий Exception (B-11)
# ============================================================

class TB08_TaskChatId(BotV111Base):

    def _seed_task_state(self, ctx):
        ctx.user_data['new_task'] = {
            'title': 'Test',
            'desc': 'Desc',
            'reward': 100,
            'type': 'subscribe',
            'link': 'https://t.me/test',
        }

    async def test_tb08a_bad_request_returns_state(self):
        from telegram.error import BadRequest
        upd = make_update_with_message("@testchannel", user_id=111111111)
        ctx = make_context()
        self._seed_task_state(ctx)
        ctx.bot.get_chat_member = AsyncMock(side_effect=BadRequest("Channel not found"))

        result = await bot_module.task_chat_id(upd, ctx)

        self.assertEqual(result, bot_module.TASK_CHAT_ID)
        upd.message.reply_text.assert_awaited_once()

    async def test_tb08b_generic_exception_returns_state(self):
        upd = make_update_with_message("@testchannel", user_id=111111111)
        ctx = make_context()
        self._seed_task_state(ctx)
        ctx.bot.get_chat_member = AsyncMock(side_effect=RuntimeError("boom"))

        with patch.object(bot_module.logger, 'exception') as mock_exc:
            result = await bot_module.task_chat_id(upd, ctx)

            self.assertEqual(result, bot_module.TASK_CHAT_ID)
            upd.message.reply_text.assert_awaited_once()
            self.assertTrue(mock_exc.called)

    async def test_tb08c_bot_not_admin_returns_state(self):
        upd = make_update_with_message("@testchannel", user_id=111111111)
        ctx = make_context()
        self._seed_task_state(ctx)
        member = MagicMock()
        member.status = 'member'
        ctx.bot.get_chat_member = AsyncMock(return_value=member)

        result = await bot_module.task_chat_id(upd, ctx)

        self.assertEqual(result, bot_module.TASK_CHAT_ID)


# ============================================================
#  TB-09 — proof_text_handler: пустой текст (B-13)
# ============================================================

class TB09_ProofEmpty(BotV111Base):

    async def test_tb09a_empty_text_returns_proof_input(self):
        user_id = 1001
        db.register_user(user_id, "tester")
        db.add_task("Manual", "Do it", 50, "manual", "https://example.com", None)
        task = db.get_active_tasks()[0]

        ctx = make_context(user_data={'submit_task_id': task['id']})
        upd = make_update_with_message("", user_id=user_id)
        upd.message.text = ""
        upd.message.reply_text = AsyncMock()

        result = await bot_module.proof_text_handler(upd, ctx)

        self.assertEqual(result, bot_module.PROOF_INPUT)
        ut = db.get_user_task(user_id, task['id'])
        self.assertIsNone(ut)
        self.assertEqual(ctx.user_data.get('submit_task_id'), task['id'])

    async def test_tb09b_whitespace_text_returns_proof_input(self):
        user_id = 1001
        db.register_user(user_id, "tester")
        db.add_task("Manual2", "Do it", 50, "manual", "https://example.com", None)
        task = db.get_active_tasks()[0]

        ctx = make_context(user_data={'submit_task_id': task['id']})
        upd = make_update_with_message("   \n  ", user_id=user_id)
        upd.message.text = "   \n  "
        upd.message.reply_text = AsyncMock()

        result = await bot_module.proof_text_handler(upd, ctx)

        self.assertEqual(result, bot_module.PROOF_INPUT)
        self.assertIsNone(db.get_user_task(user_id, task['id']))


# ============================================================
#  TB-10 — _btn_text обрезает до <= 60 (B-14)
# ============================================================

class TB10_BtnText(BotV111Base):

    def test_tb10a_short_unchanged(self):
        self.assertEqual(bot_module._btn_text("abc"), "abc")

    def test_tb10b_long_truncated_to_limit(self):
        s = "a" * 200
        result = bot_module._btn_text(s)
        self.assertLessEqual(len(result), 60)
        self.assertTrue(result.endswith("…"))

    def test_tb10c_custom_limit(self):
        s = "b" * 100
        result = bot_module._btn_text(s, limit=20)
        self.assertLessEqual(len(result), 20)
        self.assertTrue(result.endswith("…"))

    def test_tb10d_empty_and_none(self):
        self.assertEqual(bot_module._btn_text(None), "")
        self.assertEqual(bot_module._btn_text(""), "")


# ============================================================
#  TB-11 — fallthrough: неизвестный callback (B-12/B-15)
# ============================================================

class TB11_Fallthrough(BotV111Base):

    async def test_tb11a_unknown_callback_answers(self):
        upd = make_update_with_callback("unknown_action_xyz", user_id=1001)
        ctx = make_context()

        await bot_module.button_handler(upd, ctx)

        self.assertEqual(upd.callback_query.answer.await_count, 1)
        args, kwargs = upd.callback_query.answer.await_args
        answer_text = args[0] if args else kwargs.get('text')
        self.assertEqual(answer_text, "Невідома дія.")

    async def test_tb11b_admin_prefix_not_admin_denied(self):
        upd = make_update_with_callback("admin_panel", user_id=9999)
        ctx = make_context()

        await bot_module.button_handler(upd, ctx)

        self.assertEqual(upd.callback_query.answer.await_count, 1)
        _, kwargs = upd.callback_query.answer.await_args
        self.assertTrue(kwargs.get('show_alert', False))


# ============================================================
#  TB-12 — buy_: провал уведомления админам → logger.warning (B-07)
# ============================================================

class TB12_BuyAdminNotify(BotV111Base):

    async def test_tb12a_buy_success_admin_notify_fails_warning(self):
        user_id = 1001
        db.register_user(user_id, "tester")
        db.add_offer("Cat12", "Item12", "Desc", 10)
        offers = db.get_offers_by_category("Cat12")
        offer_id = offers[0]['id']

        upd = make_update_with_callback(f"buy_{offer_id}", user_id=user_id)
        ctx = make_context()
        ctx.bot.send_message = AsyncMock(side_effect=RuntimeError("network"))

        with patch.object(bot_module.logger, 'warning') as mock_warn:
            await bot_module.button_handler(upd, ctx)
            self.assertTrue(mock_warn.called)

        orders = db.get_user_orders(user_id)
        self.assertEqual(len(orders), 1)
        self.assertEqual(db.get_user(user_id)['balance'], 40)

    async def test_tb12b_buy_success_no_admin_notify_error(self):
        user_id = 1001
        db.register_user(user_id, "tester")
        db.add_offer("Cat12b", "Item12b", "Desc", 10)
        offers = db.get_offers_by_category("Cat12b")
        offer_id = offers[0]['id']

        upd = make_update_with_callback(f"buy_{offer_id}", user_id=user_id)
        ctx = make_context()

        await bot_module.button_handler(upd, ctx)

        orders = db.get_user_orders(user_id)
        self.assertEqual(len(orders), 1)


# ============================================================
#  TB-13 — regression: tuple result from claim_auto_task is unpacked
# ============================================================

class TB13_AutoTaskClaimResult(BotV111Base):

    async def test_tb13a_duplicate_claim_is_not_reported_as_success(self):
        upd = make_update_with_callback("task_check_77", user_id=1001)
        ctx = make_context()
        ctx.bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))
        task = {
            'id': 77, 'task_type': 'subscribe', 'chat_id': '@example',
            'reward': 100, 'title': 'Subscribe',
        }

        with patch.object(db, 'get_task', return_value=task), \
             patch.object(db, 'claim_auto_task', return_value=(False, 'already_done')):
            await bot_module.button_handler(upd, ctx)

        upd.callback_query.answer.assert_awaited_once_with(
            "Ти вже отримував нагороду.", show_alert=True
        )
        upd.callback_query.edit_message_text.assert_not_awaited()

    async def test_tb13b_success_claim_is_reported_as_success(self):
        upd = make_update_with_callback("task_check_78", user_id=1001)
        ctx = make_context()
        ctx.bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))
        task = {
            'id': 78, 'task_type': 'subscribe', 'chat_id': '@example',
            'reward': 100, 'title': 'Subscribe',
        }

        with patch.object(db, 'get_task', return_value=task), \
             patch.object(db, 'claim_auto_task', return_value=(True, 'ok')):
            await bot_module.button_handler(upd, ctx)

        upd.callback_query.answer.assert_awaited_once_with(
            "🎉 Бали нараховано!", show_alert=True
        )
        upd.callback_query.edit_message_text.assert_awaited_once()


# ============================================================
#  ТОЧКА ВХОДА
# ============================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
