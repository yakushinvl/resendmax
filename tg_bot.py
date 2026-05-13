import os
import random
import string
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
import database

dp = Dispatcher()
ADMIN_ID = 499223756

# Глобальные настройки
max_client_instance = None
tg_token = os.getenv("TG_TOKEN")
bot = Bot(token=tg_token)

def generate_code(length=8):
    """Генерирует код привязки"""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def get_main_keyboard(user_id: int):
    """Главное меню"""
    kb = [[KeyboardButton(text="Подключить чат", style="success"), KeyboardButton(text="Мои чаты", style="primary")]]
    if user_id == ADMIN_ID:
        kb.append([KeyboardButton(text="Админ-панель", style="danger")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

@dp.message(Command("start", "menu"))
async def start_handler(message: types.Message):
    await message.answer("Ты в меню:", reply_markup=get_main_keyboard(message.from_user.id))

@dp.message(F.text == "Админ-панель")
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
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
        "Изменить лимит привязок: limit [USER_ID] [COUNT]")
    await message.answer(text)

@dp.message(F.text == "Подключить чат")
async def connect_request(message: types.Message):
    if len(database.get_user_mappings(message.from_user.id, "tg")) >= database.get_user_limit(message.from_user.id, "tg"):
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
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отключить", callback_data=f"del_{m.id}", style="danger")]])
        topic_str = f"\nТопик ID: {m.target_thread_id}" if m.target_thread_id else ""
        await message.answer(f"📍 {m.max_chat_title}\nID MAX: {m.max_chat_id}\nID TG: {m.target_chat_id}{topic_str}", reply_markup=kb)


@dp.callback_query(F.data.startswith("del_"))
async def delete_callback(callback: types.CallbackQuery):
    database.delete_chat_mapping(int(callback.data.split("_")[1]))
    await callback.answer("Чат отключён")
    await callback.message.delete()

@dp.message(F.chat.type == "private")
async def private_handler(message: types.Message):
    if not message.text: return
    uid = message.from_user.id

    if uid == ADMIN_ID and message.text.lower().startswith("limit "):
        try:
            _, tid, count = message.text.split()
            database.set_user_limit(int(tid), "tg", int(count))
            await message.answer(f"Лимит для {tid} установлен на {count}")
        except:
            pass
        return

    if "max.ru/join/" in message.text:
        if len(database.get_user_mappings(uid, "tg")) >= database.get_user_limit(uid, "tg"):
            await message.answer("Достигнут лимит подключённых чатов")
            return
        try:
            chat = await max_client_instance.join_chat(message.text.strip())
            code = generate_code()
            database.add_pending_connection(code, chat['id'], chat.get('title', 'Без названия'), uid, "tg")
            await message.answer(f"Я зашел в «{chat.get('title')}». Для привязки группового чата добавь меня в этот чат в Telegram и введи этот код: <code>{code}</code>", parse_mode="HTML")
        except Exception as e:
            await message.answer(f"Ошибка: {e}")

@dp.message((F.chat.type == "group") | (F.chat.type == "supergroup"))
async def chat_handler(message: types.Message):
    if not message.text: return
    code = message.text.strip().upper()
    pending = database.pop_pending_connection(code)
    if pending:
        thread_id = message.message_thread_id
        if database.add_chat_mapping(pending.max_chat_id, pending.max_chat_title, "tg", message.chat.id, pending.user_platform_id, "tg", thread_id):
            await message.answer(f"Чат '{pending.max_chat_title}' привязан!")
        else: await message.answer("Этот чат уже привязан к этой группе.")

async def send_to_tg(chat_id: int, text: str, media: list = None, thread_id: int = None):
    """Отправляет сообщение в Telegram"""
    if not media:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", message_thread_id=thread_id)
    else:
        for i, m in enumerate(media):
            if i == 0: m.caption, m.parse_mode = text, "HTML"
        await bot.send_media_group(chat_id=chat_id, media=media, message_thread_id=thread_id)

