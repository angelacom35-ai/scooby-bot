"""
bot.py — интерфейс Скубі • Кеш. Версия: v1.1.11 RC.
Совместимость: python-telegram-bot 21.1.1, database.py v1.1.10 STABLE.

Пакет исправлений: B-01 … B-15 (см. отчёт аудита).
Остаточный риск B-09 (повтор notify) — известен и задокументирован в retry_notify_.
"""

import logging
import traceback
import html
import sys
import asyncio
from urllib.parse import urlparse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)

import config
import database as db

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

ADD_CATEGORY, ADD_TITLE, ADD_DESC, ADD_COST = range(4)
TASK_TITLE, TASK_DESC, TASK_TYPE, TASK_LINK, TASK_CHAT_ID, TASK_REWARD = range(10, 16)
PROOF_INPUT = 20

RETRYABLE_STATUSES = ('not_sent', 'failed', 'user_blocked', 'user_deleted')


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def _btn_text(text: str, limit: int = 60) -> str:
    """Гарантирует, что итоговый текст inline-кнопки <= limit."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


async def get_bot_username(context) -> str:
    if 'bot_username' not in context.bot_data:
        me = await context.bot.get_me()
        context.bot_data['bot_username'] = me.username
    return context.bot_data['bot_username']


def get_main_menu(user_id: int):
    keyboard = [
        [InlineKeyboardButton('👤 Мій профіль', callback_data='profile'),
         InlineKeyboardButton('💎 Заробити', callback_data='earn')],
        [InlineKeyboardButton('🎁 Каталог', callback_data='catalog'),
         InlineKeyboardButton('📦 Мої замовлення', callback_data='my_orders')],
        [InlineKeyboardButton('🚐 Собрать банду', callback_data='referral'),
         InlineKeyboardButton('🆘 Допомога', callback_data='help')],
    ]
    if is_admin(user_id):
        keyboard.append([InlineKeyboardButton('🛠 АДМІН-ПАНЕЛЬ', callback_data='admin_panel')])
    return InlineKeyboardMarkup(keyboard)


def get_back_button(callback='main'):
    return InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ Назад', callback_data=callback)]])


def get_cancel_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton('❌ Скасувати', callback_data='cancel_dialog')]])


def clear_dialog_state(context):
    for key in ['submit_task_id', 'new_offer', 'new_task']:
        context.user_data.pop(key, None)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_dialog_state(context)

    user = update.effective_user
    ref_code = context.args[0] if context.args else None
    name = user.username or user.first_name or "Боец"
    is_new, referrer_id = db.register_user(user.id, name, ref_code)

    text = "🕵️ <b>Скубі • Кеш 🚐</b>\n\nЙо, шукач пригод! 👀\n"
    if is_new:
        text += f"🎁 Твій стартовий бонус: <b>{config.START_BONUS} балів</b>\n\n"
        if referrer_id:
            try:
                await context.bot.send_message(
                    referrer_id,
                    f"🎉 По твоїй лінці зайшов новий боєць!\nНараховано <b>{config.REFERRAL_BONUS} балів</b>.",
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.warning(f"start: не удалось уведомить реферера {referrer_id}: {e}")
    else:
        text += "З поверненням у штаб!\n\n"
    text += "Обирай, куди рухаємось 👇"
    await update.message.reply_text(text, reply_markup=get_main_menu(user.id),
                                    parse_mode=ParseMode.HTML)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    answered = False

    async def ans(text: str | None = None, alert: bool = False):
        nonlocal answered
        if answered:
            return
        answered = True
        if text is None:
            await query.answer()
        else:
            await query.answer(text, show_alert=alert)

    if data == 'cancel_dialog':
        await ans()
        clear_dialog_state(context)
        await query.edit_message_text("Дію скасовано.", reply_markup=get_main_menu(user_id))
        return

    admin_prefixes = (
        'admin_', 'manage_order_', 'complete_order_', 'retry_notify_',
        'approve_task_', 'reject_task_', 'manage_task_',
    )
    if data.startswith(admin_prefixes) and not is_admin(user_id):
        await ans("⛔ Недостатньо прав.", alert=True)
        return

    try:
        if data == 'main':
            await ans()
            await query.edit_message_text(
                "🕵️ <b>Головний штаб</b>\n\nКуди вирушаємо далі? 👇",
                reply_markup=get_main_menu(user_id),
                parse_mode=ParseMode.HTML,
            )
            return

        if data == 'profile':
            await ans()
            user = db.get_user(user_id)
            if not user:
                await query.edit_message_text("Натисни /start", reply_markup=get_back_button())
                return
            safe_name = html.escape(user['username']) if user['username'] else "Боец"
            text = (
                f"👤 <b>Твій профіль</b>\n\n"
                f"🆔 ID: <code>{user['user_id']}</code>\n"
                f"📛 Нікнейм: {safe_name}\n"
                f"💰 Баланс: <b>{user['balance']} балів</b>\n"
                f"📅 Реєстрація: {user['reg_date'][:10]}\n"
            )
            await query.edit_message_text(text, reply_markup=get_back_button(),
                                          parse_mode=ParseMode.HTML)
            return

        if data == 'referral':
            await ans()
            bot_username = await get_bot_username(context)
            link = f"https://t.me/{bot_username}?start={user_id}"
            text = (
                f"🚐 <b>Собери банду!</b>\n\n"
                f"За кожного нового пацана/дівчину по твоїй лінці ти отримаєш "
                f"<b>{config.REFERRAL_BONUS} балів</b>.\n\n"
                f"Твоя лінка:\n<code>{link}</code>"
            )
            await query.edit_message_text(text, reply_markup=get_back_button(),
                                          parse_mode=ParseMode.HTML)
            return

        if data == 'earn':
            await ans()
            tasks = db.get_active_tasks()
            if not tasks:
                await query.edit_message_text("💎 Завдань поки немає.",
                                              reply_markup=get_back_button(),
                                              parse_mode=ParseMode.HTML)
                return
            keyboard = []
            for t in tasks:
                ut = db.get_user_task(user_id, t['id'])
                icon = "✅ " if ut and ut['status'] == 'approved' else ("⏳ " if ut else "🔹 ")
                btn = _btn_text(f"{icon}{t['title']} (+{t['reward']} б.)", limit=60)
                keyboard.append([InlineKeyboardButton(
                    btn,
                    callback_data=f"task_view_{t['id']}",
                )])
            keyboard.append([InlineKeyboardButton('⬅️ Назад', callback_data='main')])
            await query.edit_message_text("💎 <b>Доступні завдання:</b>",
                                          reply_markup=InlineKeyboardMarkup(keyboard),
                                          parse_mode=ParseMode.HTML)
            return

        if data.startswith('task_view_'):
            try:
                task_id = int(data.split('_')[2])
            except (ValueError, IndexError):
                await ans("Некоректна дія.", alert=False)
                return
            await ans()
            task = db.get_task(task_id)
            if not task:
                await query.edit_message_text("Завдання не знайдено.",
                                              reply_markup=get_back_button('earn'))
                return

            ut = db.get_user_task(user_id, task_id)
            text = (
                f"📋 <b>{html.escape(task['title'])}</b>\n\n"
                f"{html.escape(task['description'])}\n\n"
                f"💰 Нагорода: <b>{task['reward']} балів</b>"
            )
            keyboard = []
            if not ut or ut['status'] == 'rejected':
                keyboard.append([InlineKeyboardButton('🔗 Перейти до виконання', url=task['link'])])
                if task['task_type'] == 'subscribe':
                    keyboard.append([InlineKeyboardButton('✅ Перевірити підписку',
                                                          callback_data=f"task_check_{task_id}")])
                else:
                    keyboard.append([InlineKeyboardButton('📤 Відправити на перевірку',
                                                          callback_data=f"task_submit_{task_id}")])
            elif ut['status'] == 'pending':
                text += "\n\n⏳ <i>Заявка на перевірці у адміна.</i>"
            elif ut['status'] == 'approved':
                text += "\n\n✅ <i>Завдання виконано!</i>"
            keyboard.append([InlineKeyboardButton('⬅️ Назад', callback_data='earn')])
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                          parse_mode=ParseMode.HTML)
            return

        if data.startswith('task_check_'):
            try:
                task_id = int(data.split('_')[2])
            except (ValueError, IndexError):
                await ans("Некоректна дія.", alert=False)
                return
            task = db.get_task(task_id)
            if not task or task['task_type'] != 'subscribe':
                await ans("Завдання недоступне.", alert=False)
                return

            try:
                member = await context.bot.get_chat_member(chat_id=task['chat_id'],
                                                           user_id=user_id)
                is_sub = (
                    member.status in ['member', 'administrator', 'creator']
                    or (member.status == 'restricted' and member.is_member)
                )
                if is_sub:
                    claimed, claim_status = db.claim_auto_task(user_id, task_id)
                    if claimed:
                        await ans("🎉 Бали нараховано!", alert=True)
                        await query.edit_message_text(
                            f"✅ <b>Успіх!</b>\nТобі нараховано {task['reward']} балів!",
                            reply_markup=get_back_button('earn'),
                            parse_mode=ParseMode.HTML,
                        )
                    elif claim_status == 'already_done':
                        await ans("Ти вже отримував нагороду.", alert=True)
                    else:
                        logger.warning(
                            "claim_auto_task rejected: user_id=%s task_id=%s status=%s",
                            user_id, task_id, claim_status,
                        )
                        await ans("Не вдалося нарахувати бали. Спробуй пізніше.", alert=True)
                else:
                    await ans("❌ Ти ще не підписався на канал!", alert=True)
            except BadRequest as e:
                logger.error(f"API Error check sub: {e}")
                await ans("❌ Бот не адмін каналу або канал видалено.", alert=True)
            return

        if data == 'catalog':
            await ans()
            categories = db.get_categories()
            if not categories:
                await query.edit_message_text("Каталог порожній.",
                                              reply_markup=get_back_button())
                return
            keyboard = []
            for c in categories:
                btn = _btn_text(c['category'], limit=60)
                keyboard.append([InlineKeyboardButton(
                    btn,
                    callback_data=f'cat_{db.category_key(c["category"])}',
                )])
            keyboard.append([InlineKeyboardButton('⬅️ Назад', callback_data='main')])
            await query.edit_message_text("🎁 <b>Обери категорію:</b>",
                                          reply_markup=InlineKeyboardMarkup(keyboard),
                                          parse_mode=ParseMode.HTML)
            return

        if data.startswith('cat_'):
            await ans()
            cat_hash = data[4:]
            category = db.get_category_by_key(cat_hash)
            if not category:
                await query.edit_message_text("Категорія не знайдена.",
                                              reply_markup=get_back_button('catalog'))
                return
            offers = db.get_offers_by_category(category)
            if not offers:
                await query.edit_message_text("Товарів немає.",
                                              reply_markup=get_back_button('catalog'))
                return
            keyboard = []
            for o in offers:
                btn = _btn_text(f"{o['title']} — {o['cost']} б.", limit=60)
                keyboard.append([InlineKeyboardButton(
                    btn,
                    callback_data=f'offer_{o["id"]}',
                )])
            keyboard.append([InlineKeyboardButton('⬅️ Назад', callback_data='catalog')])
            await query.edit_message_text(f"🎁 <b>{html.escape(category)}</b>",
                                          reply_markup=InlineKeyboardMarkup(keyboard),
                                          parse_mode=ParseMode.HTML)
            return

        if data.startswith('offer_'):
            try:
                oid = int(data.split('_')[1])
            except (ValueError, IndexError):
                await ans("Некоректна дія.", alert=False)
                return
            await ans()
            offer = db.get_offer(oid)
            if not offer:
                await query.edit_message_text("Товар не знайдено.",
                                              reply_markup=get_back_button('catalog'))
                return
            text = (
                f"🎁 <b>{html.escape(offer['title'])}</b>\n\n"
                f"{html.escape(offer['description'])}\n\n"
                f"💰 Ціна: <b>{offer['cost']} балів</b>"
            )
            keyboard = [
                [InlineKeyboardButton('🛒 Купити', callback_data=f'buy_{oid}')],
                [InlineKeyboardButton('⬅️ Назад',
                                      callback_data=f'cat_{db.category_key(offer["category"])}')],
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                          parse_mode=ParseMode.HTML)
            return

        if data.startswith('buy_'):
            try:
                oid = int(data.split('_')[1])
            except (ValueError, IndexError):
                await ans("Некоректна дія.", alert=False)
                return
            await ans()
            success, msg = db.create_order(user_id, oid)
            if success:
                for admin_id in config.ADMIN_IDS:
                    try:
                        await context.bot.send_message(
                            admin_id,
                            f"🚨 <b>НОВЫЙ ЗАКАЗ!</b>\nЮзер: <code>{user_id}</code>\n{html.escape(msg)}",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception as e:
                        logger.warning(f"buy_: не удалось уведомить админа {admin_id}: {e}")
                await query.edit_message_text(f"✅ {html.escape(msg)}",
                                              reply_markup=get_back_button('main'),
                                              parse_mode=ParseMode.HTML)
            else:
                await query.edit_message_text(f"❌ {html.escape(msg)}",
                                              reply_markup=get_back_button('main'),
                                              parse_mode=ParseMode.HTML)
            return

        if data == 'my_orders':
            await ans()
            orders = db.get_user_orders(user_id)
            if not orders:
                await query.edit_message_text("📦 Замовлень немає.",
                                              reply_markup=get_back_button())
                return
            lines = []
            for o in orders:
                emoji = '⏳' if o['status'] == 'pending' else '✅'
                lines.append(f"{emoji} №{o['id']} — {html.escape(o['offer_title'])}")
            text = "📦 <b>Останні замовлення:</b>\n\n" + "\n".join(lines)
            await query.edit_message_text(text, reply_markup=get_back_button(),
                                          parse_mode=ParseMode.HTML)
            return

        if data == 'help':
            await ans()
            await query.edit_message_text(
                f"🆘 Підтримка: @HapToon\nID: <code>{user_id}</code>",
                reply_markup=get_back_button(),
                parse_mode=ParseMode.HTML,
            )
            return

        if data == 'admin_panel':
            await ans()
            keyboard = [
                [InlineKeyboardButton('➕ Додати товар', callback_data='admin_add_offer'),
                 InlineKeyboardButton('➕ Створити завдання', callback_data='admin_add_task')],
                [InlineKeyboardButton('📦 Замовлення', callback_data='admin_orders'),
                 InlineKeyboardButton('⏳ Ручні завдання', callback_data='admin_pending_tasks')],
                [InlineKeyboardButton('⬅️ Назад', callback_data='main')],
            ]
            await query.edit_message_text("🛠 <b>Адмін-панель</b>",
                                          reply_markup=InlineKeyboardMarkup(keyboard),
                                          parse_mode=ParseMode.HTML)
            return

        if data == 'admin_pending_tasks':
            await ans()
            pending = db.get_pending_user_tasks()
            if not pending:
                await query.edit_message_text("Немає заявок.",
                                              reply_markup=get_back_button('admin_panel'))
                return
            keyboard = []
            for p in pending:
                btn = _btn_text(f"👤 {p['user_id']} - {p['title']}", limit=60)
                keyboard.append([InlineKeyboardButton(
                    btn,
                    callback_data=f"manage_task_{p['user_id']}_{p['task_id']}",
                )])
            keyboard.append([InlineKeyboardButton('⬅️ Назад', callback_data='admin_panel')])
            await query.edit_message_text("⏳ <b>Заявки:</b>",
                                          reply_markup=InlineKeyboardMarkup(keyboard),
                                          parse_mode=ParseMode.HTML)
            return

        if data.startswith('manage_task_'):
            await ans()
            try:
                _, _, uid, tid = data.split('_')
                uid_int, tid_int = int(uid), int(tid)
                if uid_int <= 0 or tid_int <= 0:
                    raise ValueError
            except (ValueError, IndexError):
                await query.edit_message_text("Некоректна дія.",
                                              reply_markup=get_back_button('admin_pending_tasks'))
                return
            with db.db() as c:
                row = c.execute(
                    'SELECT ut.proof_text, ut.proof_photo, t.title '
                    'FROM user_tasks ut JOIN tasks t ON ut.task_id = t.id '
                    'WHERE ut.user_id=? AND ut.task_id=?',
                    (uid_int, tid_int),
                ).fetchone()
            if not row:
                await query.edit_message_text("Заявка не найдена.",
                                              reply_markup=get_back_button('admin_pending_tasks'))
                return
            proof_text = html.escape(row['proof_text']) if row['proof_text'] else "<i>не предоставлено</i>"
            proof_photo = row['proof_photo']
            keyboard = [
                [InlineKeyboardButton('✅ Одобрити', callback_data=f'approve_task_{uid}_{tid}'),
                 InlineKeyboardButton('❌ Відхилити', callback_data=f'reject_task_{uid}_{tid}')],
                [InlineKeyboardButton('⬅️ Назад', callback_data='admin_pending_tasks')],
            ]
            if proof_photo:
                try:
                    await context.bot.send_photo(
                        chat_id=user_id, photo=proof_photo,
                        caption=f"📎 Доказательство от <code>{uid}</code>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as e:
                    logger.warning(f"manage_task_: не удалось отправить фото {uid}/{tid}: {e}")
            await query.edit_message_text(
                f"Заявка от <code>{uid}</code> на «{html.escape(row['title'])}»\n\n"
                f"📎 <b>Текст:</b>\n{proof_text}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML,
            )
            return

        if data.startswith('approve_task_'):
            try:
                _, _, uid, tid = data.split('_')
                uid_int, tid_int = int(uid), int(tid)
                if uid_int <= 0 or tid_int <= 0:
                    raise ValueError
            except (ValueError, IndexError):
                await ans("Некоректна дія.", alert=True)
                return
            if db.approve_manual_task(uid_int, tid_int, admin_id=user_id):
                await ans("✅ Схвалено", alert=True)
                try:
                    await context.bot.send_message(uid_int,
                        "✅ Твоє ручне завдання схвалено! Бали нараховано.")
                except Exception as e:
                    logger.warning(f"approve_task_: не удалось уведомить {uid_int}: {e}")
                await query.edit_message_text("✅ Схвалено!",
                                              reply_markup=get_back_button('admin_pending_tasks'))
            else:
                await ans("Помилка: заявка вже оброблена.", alert=True)
            return

        if data.startswith('reject_task_'):
            try:
                _, _, uid, tid = data.split('_')
                uid_int, tid_int = int(uid), int(tid)
                if uid_int <= 0 or tid_int <= 0:
                    raise ValueError
            except (ValueError, IndexError):
                await ans("Некоректна дія.", alert=True)
                return
            if db.reject_manual_task(uid_int, tid_int, admin_id=user_id):
                await ans("❌ Відхилено", alert=True)
                try:
                    await context.bot.send_message(uid_int,
                        "❌ Твоє ручне завдання відхилено. Можеш спробувати ще раз.")
                except Exception as e:
                    logger.warning(f"reject_task_: не удалось уведомить {uid_int}: {e}")
                await query.edit_message_text("❌ Відхилено.",
                                              reply_markup=get_back_button('admin_pending_tasks'))
            else:
                await ans("Помилка: заявка вже оброблена.", alert=True)
            return

        if data == 'admin_orders':
            await ans()
            pending = db.get_pending_orders()
            with db.db() as c:
                failed = c.execute(
                    """SELECT * FROM orders
                       WHERE status='completed'
                         AND notification_status IN ('not_sent','failed','user_blocked','user_deleted')
                       ORDER BY id DESC LIMIT 30"""
                ).fetchall()
            if not pending and not failed:
                await query.edit_message_text("Немає замовлень.",
                                              reply_markup=get_back_button('admin_panel'))
                return
            keyboard = []
            for o in pending[:30]:
                btn = _btn_text(f"📦 №{o['id']} · {o['offer_title']}", limit=60)
                keyboard.append([InlineKeyboardButton(
                    btn,
                    callback_data=f"manage_order_{o['id']}",
                )])
            for o in failed[:10]:
                btn = _btn_text(f"🔁 №{o['id']} · {o['offer_title']}", limit=60)
                keyboard.append([InlineKeyboardButton(
                    btn,
                    callback_data=f"manage_order_{o['id']}",
                )])
            keyboard.append([InlineKeyboardButton('⬅️ Назад', callback_data='admin_panel')])
            await query.edit_message_text(
                f"📦 <b>Замовлення:</b> pending={len(pending)}, retry={len(failed)}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML,
            )
            return

        if data.startswith('manage_order_'):
            try:
                oid = int(data.split('_')[2])
            except (ValueError, IndexError):
                await ans("Некоректна дія.", alert=False)
                return
            await ans()
            order = db.get_order(oid)
            if not order:
                await query.edit_message_text("Замовлення не знайдено.",
                                              reply_markup=get_back_button('admin_orders'))
                return
            keyboard = []
            if order['status'] == 'pending':
                keyboard.append([InlineKeyboardButton('✅ Виконано',
                                                      callback_data=f'complete_order_{oid}')])
            elif order['status'] == 'completed' and order['notification_status'] in RETRYABLE_STATUSES:
                keyboard.append([InlineKeyboardButton('🔄 Повторити сповіщення',
                                                      callback_data=f'retry_notify_{oid}')])
            keyboard.append([InlineKeyboardButton('⬅️ Назад', callback_data='admin_orders')])
            await query.edit_message_text(
                f"📦 №{oid}\nЮзер: <code>{order['user_id']}</code>\n"
                f"Статус: {order['status']} / {order['notification_status']}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML,
            )
            return

        if data.startswith('complete_order_'):
            try:
                oid = int(data.split('_')[2])
            except (ValueError, IndexError):
                await ans("Некоректна дія.", alert=True)
                return
            order = db.get_order(oid)
            if not order:
                await ans("Замовлення не знайдено.", alert=True)
                return
            if not db.complete_order(oid, admin_id=user_id):
                await ans("Замовлення вже виконано.", alert=True)
                return

            await ans("✅ Виконано", alert=True)

            send_status = None
            try:
                await context.bot.send_message(order['user_id'], "✅ Замовлення виконано!")
            except Forbidden:
                send_status = 'user_blocked'
            except BadRequest as e:
                if 'chat not found' in str(e).lower():
                    send_status = 'user_deleted'
                else:
                    send_status = 'failed'
                    logger.warning(f"complete_order: BadRequest {oid}: {e}")
            except TelegramError as e:
                send_status = 'failed'
                logger.warning(f"complete_order: TelegramError {oid}: {e}")
            except Exception as e:
                send_status = 'failed'
                logger.exception(f"complete_order: сбой отправки {oid}: {e}")

            try:
                db.update_notification_status(oid, 'sent' if send_status is None else send_status)
            except Exception as e:
                logger.critical(f"complete_order: НЕ УДАЛОСЬ записать status для {oid}: {e}")

            await query.edit_message_text("✅ Виконано.",
                                          reply_markup=get_back_button('admin_orders'))
            return

        if data.startswith('retry_notify_'):
            try:
                oid = int(data.split('_')[2])
            except (ValueError, IndexError):
                await ans("Некоректна дія.", alert=True)
                return
            order = db.get_order(oid)
            if not order or order['status'] != 'completed':
                await ans("Замовлення не знайдено або не виконано.", alert=True)
                return
            if order['notification_status'] not in RETRYABLE_STATUSES:
                await ans("Повтор не потрібен.", alert=True)
                return

            await ans("Надсилаю…", alert=False)

            send_status = None
            try:
                await context.bot.send_message(order['user_id'], "✅ Замовлення виконано!")
            except Forbidden:
                send_status = 'user_blocked'
            except BadRequest as e:
                if 'chat not found' in str(e).lower():
                    send_status = 'user_deleted'
                else:
                    send_status = 'failed'
                    logger.warning(f"retry_notify: BadRequest {oid}: {e}")
            except TelegramError as e:
                send_status = 'failed'
                logger.warning(f"retry_notify: TelegramError {oid}: {e}")
            except Exception as e:
                send_status = 'failed'
                logger.exception(f"retry_notify: сбой {oid}: {e}")

            try:
                db.update_notification_status(oid, 'sent' if send_status is None else send_status)
            except Exception as e:
                logger.critical(f"retry_notify: НЕ УДАЛОСЬ записать status для {oid}: {e}")

            await query.edit_message_text(
                "✅ Готово." if send_status is None else "❌ Помилка надсилання.",
                reply_markup=get_back_button('admin_orders'),
            )
            return

        await ans("Невідома дія.", alert=False)
        return

    except Exception as e:
        logger.exception(f"button_handler: необработанная ошибка для data={data!r}: {e}")
        if not answered:
            try:
                await ans("Помилка. Спробуйте ще раз.", alert=False)
            except Exception:
                pass
        return


# --- ДИАЛОГИ: ДОБАВЛЕНИЕ ТОВАРА ---
async def admin_add_offer_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    context.user_data['new_offer'] = {}
    await query.message.reply_text("Категорія (до 50):", reply_markup=get_cancel_button())
    return ADD_CATEGORY


async def add_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    t = update.message.text.strip()
    if len(t) > 50 or len(t) == 0:
        await update.message.reply_text("Від 1 до 50 символів:", reply_markup=get_cancel_button())
        return ADD_CATEGORY
    context.user_data['new_offer']['cat'] = t
    await update.message.reply_text("Назва (до 100):", reply_markup=get_cancel_button())
    return ADD_TITLE


async def add_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    t = update.message.text.strip()
    if len(t) > 100 or len(t) == 0:
        await update.message.reply_text("Від 1 до 100 символів:", reply_markup=get_cancel_button())
        return ADD_TITLE
    context.user_data['new_offer']['title'] = t
    await update.message.reply_text("Опис (до 1000):", reply_markup=get_cancel_button())
    return ADD_DESC


async def add_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    t = update.message.text.strip()
    if len(t) > 1000 or len(t) == 0:
        await update.message.reply_text("Від 1 до 1000 символів:", reply_markup=get_cancel_button())
        return ADD_DESC
    context.user_data['new_offer']['desc'] = t
    await update.message.reply_text("Ціна (більше 0):", reply_markup=get_cancel_button())
    return ADD_COST


async def add_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        c = int(update.message.text.strip())
        if c <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Ціле число > 0:", reply_markup=get_cancel_button())
        return ADD_COST
    o = context.user_data['new_offer']
    try:
        db.add_offer(o['cat'], o['title'], o['desc'], c)
    except Exception as e:
        logger.exception(f"add_offer: {e}")
        await update.message.reply_text("❌ Помилка збереження товару. Спробуй ще раз.",
                                        reply_markup=get_cancel_button())
        return ADD_COST
    await update.message.reply_text(f"✅ Товар «{html.escape(o['title'])}» додано!")
    await update.message.reply_text("Меню:", reply_markup=get_main_menu(update.effective_user.id))
    context.user_data.pop('new_offer', None)
    return ConversationHandler.END


# --- ДИАЛОГИ: ДОБАВЛЕНИЕ ЗАДАНИЯ ---
async def admin_add_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    context.user_data['new_task'] = {}
    await query.message.reply_text("Назва завдання:", reply_markup=get_cancel_button())
    return TASK_TITLE


async def task_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    t = update.message.text.strip()
    if len(t) == 0:
        await update.message.reply_text("Не може бути пустою:", reply_markup=get_cancel_button())
        return TASK_TITLE
    if len(t) > 100:
        await update.message.reply_text("Максимум 100 символів. Спробуй коротше:",
                                        reply_markup=get_cancel_button())
        return TASK_TITLE
    context.user_data['new_task']['title'] = t
    await update.message.reply_text("Опис:", reply_markup=get_cancel_button())
    return TASK_DESC


async def task_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    t = update.message.text.strip()
    if len(t) == 0:
        await update.message.reply_text("Не може бути пустим:", reply_markup=get_cancel_button())
        return TASK_DESC
    if len(t) > 1000:
        await update.message.reply_text("Максимум 1000 символів. Спробуй коротше:",
                                        reply_markup=get_cancel_button())
        return TASK_DESC
    context.user_data['new_task']['desc'] = t
    await update.message.reply_text("Тип: `sub` (авто, 100 балів) або `manual` (ручна):",
                                    reply_markup=get_cancel_button())
    return TASK_TYPE


async def task_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    t = update.message.text.strip().lower()
    if t not in ['sub', 'manual']:
        await update.message.reply_text("Тільки 'sub' або 'manual':",
                                        reply_markup=get_cancel_button())
        return TASK_TYPE
    context.user_data['new_task']['type'] = 'subscribe' if t == 'sub' else 'manual'

    if t == 'sub':
        context.user_data['new_task']['reward'] = 100
        await update.message.reply_text("Посилання на канал (http/https):",
                                        reply_markup=get_cancel_button())
        return TASK_LINK
    else:
        await update.message.reply_text("Нагорода в балах (1-10000):",
                                        reply_markup=get_cancel_button())
        return TASK_REWARD


async def task_reward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        r = int(update.message.text.strip())
        if r <= 0 or r > 10000:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Число від 1 до 10000:",
                                        reply_markup=get_cancel_button())
        return TASK_REWARD
    context.user_data['new_task']['reward'] = r
    await update.message.reply_text("Посилання (http/https):",
                                    reply_markup=get_cancel_button())
    return TASK_LINK


async def task_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    url = update.message.text.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ['http', 'https'] or not parsed.netloc:
        await update.message.reply_text("Некоректне посилання (потрібен http/https та домен):",
                                        reply_markup=get_cancel_button())
        return TASK_LINK
    context.user_data['new_task']['link'] = url

    if context.user_data['new_task']['type'] == 'subscribe':
        await update.message.reply_text("Введи @username або ID каналу:",
                                        reply_markup=get_cancel_button())
        return TASK_CHAT_ID

    nt = context.user_data['new_task']
    try:
        db.add_task(nt['title'], nt['desc'], nt['reward'], 'manual', nt['link'])
    except Exception as e:
        logger.exception(f"task_link manual: {e}")
        await update.message.reply_text("❌ Помилка збереження завдання. Спробуй ще раз.",
                                        reply_markup=get_cancel_button())
        return TASK_LINK
    await update.message.reply_text("✅ Ручне завдання створено!")
    await update.message.reply_text("Меню:", reply_markup=get_main_menu(update.effective_user.id))
    context.user_data.pop('new_task', None)
    return ConversationHandler.END


async def task_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    chat_id = update.message.text.strip()

    try:
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        if bot_member.status not in ['administrator', 'creator']:
            await update.message.reply_text(
                "❌ Бот не є адміністратором цього каналу. Додай бота в адміни і спробуй ще раз:",
                reply_markup=get_cancel_button())
            return TASK_CHAT_ID
    except BadRequest as e:
        await update.message.reply_text(
            f"❌ Помилка: {e}. Перевір ID/username і спробуй ще раз:",
            reply_markup=get_cancel_button())
        return TASK_CHAT_ID
    except Exception as e:
        logger.exception(f"task_chat_id: {e}")
        await update.message.reply_text(
            "❌ Тимчасова помилка. Спробуй ще раз.",
            reply_markup=get_cancel_button())
        return TASK_CHAT_ID

    nt = context.user_data['new_task']
    try:
        db.add_task(nt['title'], nt['desc'], nt['reward'], 'subscribe', nt['link'], chat_id)
    except Exception as e:
        logger.exception(f"task_chat_id subscribe: {e}")
        await update.message.reply_text("❌ Помилка збереження завдання. Спробуй ще раз.",
                                        reply_markup=get_cancel_button())
        return TASK_CHAT_ID
    await update.message.reply_text("✅ Авто-завдання створено!")
    await update.message.reply_text("Меню:", reply_markup=get_main_menu(update.effective_user.id))
    context.user_data.pop('new_task', None)
    return ConversationHandler.END


# --- B-01: точка входа для ручных доказательств ---
async def start_proof_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        task_id = int(query.data.split('_')[2])
        if task_id <= 0:
            raise ValueError
    except (ValueError, IndexError):
        await query.answer("Некоректне завдання.", show_alert=True)
        return ConversationHandler.END

    if context.user_data.get('submit_task_id'):
        await query.answer("Ти вже відправляєш заявку.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    context.user_data['submit_task_id'] = task_id
    await query.edit_message_text(
        "📎 <b>Прикрепи доказательство</b>\n\n"
        "Отправь <b>фото</b> или <b>текст</b> (до 500 символов).\n\n"
        "Это увидит админ при проверке:",
        reply_markup=get_cancel_button(),
        parse_mode=ParseMode.HTML,
    )
    return PROOF_INPUT


async def proof_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    task_id = context.user_data.get('submit_task_id')
    if not task_id:
        return ConversationHandler.END

    proof = update.message.text.strip()[:500] if update.message.text else ""
    if not proof:
        await update.message.reply_text(
            "❌ Доказательство не может быть пустым. Отправь текст или фото.",
            reply_markup=get_cancel_button(),
        )
        return PROOF_INPUT

    try:
        success, code, msg = db.submit_manual_task(user_id, task_id, proof_text=proof)
    except Exception as e:
        logger.exception(f"proof_text_handler: {e}")
        await update.message.reply_text("❌ Помилка надсилання. Спробуй ще раз.",
                                        reply_markup=get_main_menu(user_id))
        context.user_data.pop('submit_task_id', None)
        return ConversationHandler.END

    if success:
        for admin_id in config.ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin_id,
                    f"🔔 <b>Нова заявка (текст)!</b>\nЮзер: <code>{user_id}</code>\nЗавдання ID: {task_id}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.warning(f"proof_text: не удалось уведомить админа {admin_id}: {e}")
        await update.message.reply_text(f"✅ {html.escape(msg)}",
                                        reply_markup=get_main_menu(user_id))
    else:
        await update.message.reply_text(f"❌ {html.escape(msg)}",
                                        reply_markup=get_main_menu(user_id))

    context.user_data.pop('submit_task_id', None)
    return ConversationHandler.END


async def proof_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    task_id = context.user_data.get('submit_task_id')
    if not task_id:
        return ConversationHandler.END

    if not update.message.photo:
        await update.message.reply_text("Пожалуйста, отправь фото или текст.",
                                        reply_markup=get_cancel_button())
        return PROOF_INPUT

    photo_file_id = update.message.photo[-1].file_id
    caption = update.message.caption[:500] if update.message.caption else ""

    try:
        success, code, msg = db.submit_manual_task(user_id, task_id,
                                                  proof_text=caption,
                                                  proof_photo=photo_file_id)
    except Exception as e:
        logger.exception(f"proof_photo_handler: {e}")
        await update.message.reply_text("❌ Помилка надсилання. Спробуй ще раз.",
                                        reply_markup=get_main_menu(user_id))
        context.user_data.pop('submit_task_id', None)
        return ConversationHandler.END

    if success:
        for admin_id in config.ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin_id,
                    f"🔔 <b>Нова заявка (фото)!</b>\nЮзер: <code>{user_id}</code>\nЗавдання ID: {task_id}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.warning(f"proof_photo: не удалось уведомить админа {admin_id}: {e}")
        await update.message.reply_text(f"✅ {html.escape(msg)}",
                                        reply_markup=get_main_menu(user_id))
    else:
        await update.message.reply_text(f"❌ {html.escape(msg)}",
                                        reply_markup=get_main_menu(user_id))

    context.user_data.pop('submit_task_id', None)
    return ConversationHandler.END


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_dialog_state(context)
    await update.message.reply_text("Скасовано.",
                                    reply_markup=get_main_menu(update.effective_user.id))
    return ConversationHandler.END


async def cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    clear_dialog_state(context)
    await query.edit_message_text("Скасовано.",
                                  reply_markup=get_main_menu(query.from_user.id))
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(msg="Exception:", exc_info=context.error)
    err_type = type(context.error).__name__
    err_msg = str(context.error)[:200]
    safe_text = html.escape(f"{err_type}: {err_msg}")
    for admin_id in config.ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"🚨 <b>Помилка:</b>\n<code>{safe_text}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


# --- B-02: критическое уведомление и аварийный выход ---
def _notify_admins_critical(message: str) -> None:
    """Уведомляет админов через прямой telegram.Bot, не через Application."""
    try:
        bot = Bot(token=config.BOT_TOKEN)
    except Exception as e:
        logger.critical(f"Bot: не удалось создать: {e}")
        return

    async def _send():
        init_ok = False
        try:
            await bot.initialize()
            init_ok = True
        except Exception as e:
            logger.critical(f"Bot.initialize: {e}")
        try:
            if init_ok:
                for admin_id in config.ADMIN_IDS:
                    try:
                        await bot.send_message(admin_id, message, parse_mode=ParseMode.HTML)
                    except Exception as e:
                        logger.warning(f"Bot: не удалось уведомить {admin_id}: {e}")
        finally:
            try:
                await bot.shutdown()
            except Exception as e:
                logger.critical(f"Bot.shutdown: {e}")

    try:
        asyncio.run(_send())
    except Exception as e:
        logger.critical(f"asyncio.run(_send): {e}")


def main():
    db.init_db()
    report = db.audit_integrity()
    if not report['ok']:
        msg = (
            "🚨 <b>Бот не запущен.</b>\n"
            "Аудит БД провален:\n"
            f"<code>{html.escape(str(report['issues']))[:1500]}</code>"
        )
        logger.critical(f"АУДИТ ПРОВАЛЕН: {report['issues']}")
        _notify_admins_critical(msg)
        raise SystemExit(1)
    logger.info("✅ Аудит целостности пройден.")

    app = Application.builder().token(config.BOT_TOKEN).build()

    offer_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_offer_start, pattern='^admin_add_offer$')],
        states={
            ADD_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_category)],
            ADD_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_title)],
            ADD_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_desc)],
            ADD_COST: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_cost)],
        },
        fallbacks=[
            CommandHandler('cancel', cancel_cmd),
            CallbackQueryHandler(cancel_cb, pattern='^cancel_dialog$'),
        ],
        per_message=False,
    )

    task_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_task_start, pattern='^admin_add_task$')],
        states={
            TASK_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, task_title)],
            TASK_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, task_desc)],
            TASK_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, task_type)],
            TASK_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, task_link)],
            TASK_CHAT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, task_chat_id)],
            TASK_REWARD: [MessageHandler(filters.TEXT & ~filters.COMMAND, task_reward)],
        },
        fallbacks=[
            CommandHandler('cancel', cancel_cmd),
            CallbackQueryHandler(cancel_cb, pattern='^cancel_dialog$'),
        ],
        per_message=False,
    )

    proof_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_proof_input, pattern=r'^task_submit_[1-9][0-9]*$')],
        states={
            PROOF_INPUT: [
                MessageHandler(filters.PHOTO, proof_photo_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, proof_text_handler),
            ]
        },
        fallbacks=[
            CommandHandler('cancel', cancel_cmd),
            CallbackQueryHandler(cancel_cb, pattern='^cancel_dialog$'),
        ],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(offer_conv)
    app.add_handler(task_conv)
    app.add_handler(proof_conv)
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(error_handler)

    logger.info("🚐 Скубі • Кеш v1.1.11 RC bot.py запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
