import logging
import os
import random
import string
from typing import Any, List, Optional

logger = logging.getLogger("TG_Bot")

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

import database

dp = Dispatcher()

# Подключение роутера MIREA
try:
    from mirea.tg_bot import router as mirea_router
    from mirea.db import init_db as init_mirea_db
    init_mirea_db()
    dp.include_router(mirea_router)
    logger.info("Роутер MIREA подключен к основному боту")
except Exception as e:
    logger.warning(f"Роутер MIREA не подключен: {e}")
ADMIN_ID = 499223756

# Глобальные настройки
max_client_instance = None
tg_token = os.getenv("TG_TOKEN")
bot = Bot(token=tg_token) if tg_token else None


def generate_code(length: int = 8) -> str:
    """Генерирует код привязки"""
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


def get_main_keyboard(user_id: int):
    """Главное меню"""
    kb = [[KeyboardButton(text="Подключить чат", style="success"), KeyboardButton(text="Мои чаты", style="primary")]]
    if user_id == ADMIN_ID:
        kb.append([KeyboardButton(text="Админ-панель", style="danger")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


@dp.message(Command("start", "menu"))
async def start_handler(message: types.Message):
    await message.answer("Меню управления:", reply_markup=get_main_keyboard(message.from_user.id))


@dp.message(F.text == "Админ-панель")
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    s = database.get_stats()
    text = (
        f"⚙️ Админ-панель\n\n"
        f"Всего чатов MAX: {s['unique_max_chats']}\n"
        f"Привязок:\n"
        f"- VK: {s['vk_chats']}\n"
        f"- TG: {s['tg_chats']}\n"
        f"Пользователей:\n"
        f"- VK: {s['vk_users']}\n"
        f"- TG: {s['tg_users']}\n\n"
        "Изменить лимит привязок: limit [USER_ID] [COUNT]"
    )
    await message.answer(text)


@dp.message(F.text == "Подключить чат")
async def connect_request(message: types.Message):
    if len(database.get_user_mappings(message.from_user.id, "tg")) >= database.get_user_limit(
        message.from_user.id, "tg"
    ):
        await message.answer("Достигнут лимит подключённых чатов")
        return
    await message.answer("Отправь инвайт-ссылку на чат в MAX (пример: https://max.ru/join/ABCDEFGH)")


@dp.message(F.text == "Мои чаты")
async def my_chats(message: types.Message):
    mappings = database.get_user_mappings(message.from_user.id, "tg")
    if not mappings:
        await message.answer("У тебя ещё нет подключённых чатов")
        return
    await message.answer("Все твои подключённые чаты:")
    for m in mappings:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отключить", callback_data=f"del_{m.id}", style="danger")]]
        )
        topic_str = f"\nТопик ID: {m.target_thread_id}" if m.target_thread_id else ""
        await message.answer(
            f"📍 {m.max_chat_title}\nID MAX: {m.max_chat_id}\nID TG: {m.target_chat_id}{topic_str}",
            reply_markup=kb,
        )


@dp.callback_query(F.data.startswith("del_"))
async def delete_callback(callback: types.CallbackQuery):
    database.delete_chat_mapping(int(callback.data.split("_")[1]))
    await callback.answer("Чат отключён")
    await callback.message.delete()


@dp.callback_query(F.data.startswith("m_"))
async def max_callback_handler(callback: types.CallbackQuery):
    """Ответ на нажатие дублированных инлайн-кнопок MAX"""
    await callback.answer()


@dp.message(F.chat.type == "private")
async def private_handler(message: types.Message):
    if not message.text:
        return
    uid = message.from_user.id

    if uid == ADMIN_ID and message.text.lower().startswith("limit "):
        try:
            _, tid, count = message.text.split()
            database.set_user_limit(int(tid), "tg", int(count))
            await message.answer(f"Лимит для {tid} установлен на {count}")
        except Exception:
            pass
        return

    if "max.ru/join/" in message.text:
        if len(database.get_user_mappings(uid, "tg")) >= database.get_user_limit(uid, "tg"):
            await message.answer("Достигнут лимит подключённых чатов")
            return
        try:
            chat = await max_client_instance.join_chat(message.text.strip())
            code = generate_code()
            database.add_pending_connection(
                code, chat["id"], chat.get("title", "Без названия"), uid, "tg"
            )
            await message.answer(
                f"Я зашёл в «{chat.get('title')}». Для привязки группового чата добавь меня в этот чат в Telegram и введи этот код: <code>{code}</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            await message.answer(f"Ошибка: {e}")


@dp.message((F.chat.type == "group") | (F.chat.type == "supergroup"))
async def chat_handler(message: types.Message):
    if not message.text:
        return
    code = message.text.strip().upper()
    pending = database.pop_pending_connection(code)
    if pending:
        thread_id = message.message_thread_id
        if database.add_chat_mapping(
            pending.max_chat_id,
            pending.max_chat_title,
            "tg",
            message.chat.id,
            pending.user_platform_id,
            "tg",
            thread_id,
        ):
            await message.answer(f"Чат «{pending.max_chat_title}» привязан!")
        else:
            await message.answer("Этот чат уже привязан к этой группе")


def _split_text(text: str, max_length: int = 4096) -> List[str]:
    """Разбивает длинный текст на части, сохраняя целостность строк"""
    if len(text) <= max_length:
        return [text]
    parts = []
    while text:
        if len(text) <= max_length:
            parts.append(text)
            break
        idx = text.rfind("\n", 0, max_length)
        if idx == -1:
            idx = text.rfind(" ", 0, max_length)
        if idx == -1:
            idx = max_length
        parts.append(text[:idx])
        text = text[idx:].lstrip("\n")
    return parts


def _normalize_media_item(item: Any):
    if isinstance(item, dict):
        m_type = item.get("type", "file")
        data = item.get("data", b"")
        filename = item.get("filename", "file")
        input_file = BufferedInputFile(data, filename=filename)
        if m_type == "photo":
            return "visual", "photo", input_file
        elif m_type == "video":
            return "visual", "video", input_file
        else:
            return "doc", "doc", input_file
    elif isinstance(item, InputMediaPhoto):
        return "visual", "photo", item.media
    elif isinstance(item, InputMediaVideo):
        return "visual", "video", item.media
    elif isinstance(item, InputMediaDocument):
        return "doc", "doc", item.media
    else:
        return "doc", "doc", item


async def send_to_tg(
    chat_id: int,
    text: str = "",
    media: Optional[List[Any]] = None,
    thread_id: Optional[int] = None,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
):
    """Отправляет сообщение в Telegram с поддержкой вложений, форматирования и инлайн-клавиатуры.
    Если в MAX сообщение содержало текст и вложения, они отправляются единым сообщением (в виде подписи).
    """
    if not bot:
        return

    media = media or []

    visual_items = []
    doc_items = []

    for m in media:
        cat, m_type, f = _normalize_media_item(m)
        if cat == "visual":
            visual_items.append((m_type, f))
        else:
            doc_items.append((m_type, f))

    # 1. Если вложений нет - отправляем обычным текстовым сообщением
    if not visual_items and not doc_items:
        if not text and not reply_markup:
            return
        chunks = _split_text(text, 4096) if text else [""]
        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            await bot.send_message(
                chat_id=chat_id,
                text=chunk or " ",
                parse_mode="HTML" if chunk else None,
                message_thread_id=thread_id,
                reply_markup=reply_markup if is_last else None,
            )
        return

    # 2. Если есть вложения:
    # В Telegram лимит на caption к медиа составляет 1024 символа
    caption = text if len(text) <= 1024 else ""
    followup_text = text if len(text) > 1024 else ""
    markup_sent = False

    # Сценарий А: Ровно ОДНО визуальное вложение (и нет документов)
    if len(visual_items) == 1 and not doc_items:
        m_type, f = visual_items[0]
        cur_markup = reply_markup if not followup_text else None
        if m_type == "photo":
            await bot.send_photo(
                chat_id=chat_id,
                photo=f,
                caption=caption or None,
                parse_mode="HTML" if caption else None,
                reply_markup=cur_markup,
                message_thread_id=thread_id,
            )
        else:
            await bot.send_video(
                chat_id=chat_id,
                video=f,
                caption=caption or None,
                parse_mode="HTML" if caption else None,
                reply_markup=cur_markup,
                message_thread_id=thread_id,
            )
        if cur_markup:
            markup_sent = True

    # Сценарий Б: Ровно ОДИН документ/аудио (и нет визуальных медиа)
    elif len(doc_items) == 1 and not visual_items:
        _, f = doc_items[0]
        cur_markup = reply_markup if not followup_text else None
        await bot.send_document(
            chat_id=chat_id,
            document=f,
            caption=caption or None,
            parse_mode="HTML" if caption else None,
            reply_markup=cur_markup,
            message_thread_id=thread_id,
        )
        if cur_markup:
            markup_sent = True

    # Сценарий В: Несколько вложений (альбомы фото/видео и/или документов)
    else:
        caption_assigned = False

        if visual_items:
            # Если визуальное вложение только 1, а также есть документы
            if len(visual_items) == 1:
                m_type, f = visual_items[0]
                c = caption if not caption_assigned else None
                pm = "HTML" if c else None
                if c:
                    caption_assigned = True
                if m_type == "photo":
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=f,
                        caption=c,
                        parse_mode=pm,
                        message_thread_id=thread_id,
                    )
                else:
                    await bot.send_video(
                        chat_id=chat_id,
                        video=f,
                        caption=c,
                        parse_mode=pm,
                        message_thread_id=thread_id,
                    )
            else:
                for i in range(0, len(visual_items), 10):
                    batch_raw = visual_items[i : i + 10]
                    batch = []
                    for j, (m_type, f) in enumerate(batch_raw):
                        c = caption if (not caption_assigned and j == 0) else None
                        pm = "HTML" if c else None
                        if c:
                            caption_assigned = True
                        if m_type == "photo":
                            batch.append(InputMediaPhoto(media=f, caption=c, parse_mode=pm))
                        else:
                            batch.append(InputMediaVideo(media=f, caption=c, parse_mode=pm))
                    await bot.send_media_group(
                        chat_id=chat_id,
                        media=batch,
                        message_thread_id=thread_id,
                    )

        if doc_items:
            if len(doc_items) == 1:
                _, f = doc_items[0]
                c = caption if not caption_assigned else None
                pm = "HTML" if c else None
                if c:
                    caption_assigned = True
                await bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    caption=c,
                    parse_mode=pm,
                    message_thread_id=thread_id,
                )
            else:
                for i in range(0, len(doc_items), 10):
                    batch_raw = doc_items[i : i + 10]
                    batch = []
                    for j, (_, f) in enumerate(batch_raw):
                        c = caption if (not caption_assigned and j == 0) else None
                        pm = "HTML" if c else None
                        if c:
                            caption_assigned = True
                        batch.append(InputMediaDocument(media=f, caption=c, parse_mode=pm))
                    await bot.send_media_group(
                        chat_id=chat_id,
                        media=batch,
                        message_thread_id=thread_id,
                    )

    # 3. Если текст превышал 1024 символа - отправляем полный текст отдельным сообщением
    if followup_text:
        chunks = _split_text(followup_text, 4096)
        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode="HTML",
                message_thread_id=thread_id,
                reply_markup=reply_markup if (is_last and not markup_sent) else None,
            )
            if is_last and reply_markup:
                markup_sent = True

    # 4. Если кнопки не были отправлены (например, отправлялся альбом) - отправляем их
    if reply_markup and not markup_sent:
        await bot.send_message(
            chat_id=chat_id,
            text="🔗 Кнопки к записи:",
            reply_markup=reply_markup,
            message_thread_id=thread_id,
        )


async def send_poll_to_tg(
    chat_id: int,
    question: str,
    options: List[str],
    is_anonymous: bool = True,
    allows_multiple_answers: bool = False,
    is_closed: bool = False,
    thread_id: Optional[int] = None,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
):
    """Создаёт и отправляет опрос в Telegram с приближёнными к MAX параметрами"""
    if not bot:
        return

    # Ограничения Telegram: от 2 до 10 вариантов ответа, каждый до 100 символов
    valid_options = [opt[:100] for opt in options[:10] if opt.strip()]
    if len(valid_options) < 2:
        return

    poll_params = {
        "chat_id": chat_id,
        "question": question[:300],
        "options": valid_options,
        "is_anonymous": is_anonymous,
        "allows_multiple_answers": allows_multiple_answers,
        "is_closed": is_closed,
        "message_thread_id": thread_id,
    }
    if reply_markup:
        poll_params["reply_markup"] = reply_markup

    try:
        await bot.send_poll(**poll_params)
    except Exception as e:
        logger.error(f"Ошибка отправки опроса в Telegram: {e}")

