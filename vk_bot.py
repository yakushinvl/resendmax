import os
import random
import string
import logging
from vkbottle.bot import Bot, Message
from vkbottle import API
from vkbottle import Keyboard, Text, KeyboardButtonColor, PhotoMessageUploader, DocMessagesUploader, VideoUploader
import database

logger = logging.getLogger("VK_Bot")
ADMIN_ID = 505357247

# Глобальные настройки
max_client_instance = None
vk_token = os.getenv("VK_TOKEN")
vk_user_token = os.getenv("VK_USER_TOKEN")
bot = Bot(vk_token)
photo_uploader = PhotoMessageUploader(bot.api)
doc_uploader = DocMessagesUploader(bot.api)

user_video_uploader = VideoUploader(API(vk_user_token))

_group_id = None

async def get_group_id():
    global _group_id
    if _group_id is None:
        try:
            resp = await bot.api.groups.get_by_id()
            if hasattr(resp, "groups") and resp.groups: _group_id = resp.groups[0].id
            elif isinstance(resp, list) and resp: _group_id = getattr(resp[0], "id", 0)
            else: _group_id = getattr(resp, "id", 0)
        except Exception as e:
            logger.error(f"Ошибка получения group_id: {e}")
            return 0
    return _group_id

def generate_code(length=8):
    """Генерирует код привязки"""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def get_main_keyboard(user_id: int):
    """Главное меню"""
    kb = Keyboard(one_time=False).add(Text("Подключить чат"), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text("Мои чаты"), color=KeyboardButtonColor.PRIMARY)
    if user_id == ADMIN_ID:
        kb.row().add(Text("Админ-панель"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()

@bot.on.private_message(text=["Начать", "начать", "НАЧАТЬ", "Start", "start", "START", "Меню", "меню", "МЕНЮ", "Menu", "menu", "MENU"])
async def start_handler(message: Message):
    await message.answer("Меню управления:", keyboard=get_main_keyboard(message.from_id))

@bot.on.private_message(text="Админ-панель")
async def admin_panel(message: Message):
    if message.from_id != ADMIN_ID: return
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

@bot.on.private_message(text="Подключить чат")
async def connect_request(message: Message):
    if len(database.get_user_mappings(message.from_id, "vk")) >= database.get_user_limit(message.from_id, "vk"):
        await message.answer("Достигнут лимит подключённых чатов")
        return
    await message.answer("Отправь инвайт-ссылку на чат в MAX (пример: https://max.ru/join/ABCDEFGH)")

@bot.on.private_message(text="Мои чаты")
async def my_chats(message: Message):
    mappings = database.get_user_mappings(message.from_id, "vk")
    if not mappings:
        await message.answer("У тебя ещё нет подключённых чатов")
        return
    await message.answer("Все твои подключённые чаты:")
    for m in mappings:
        kb = Keyboard(inline=True).add(Text("Отключить", payload={"cmd": "del", "id": m.id}), color=KeyboardButtonColor.NEGATIVE)
        await message.answer(f"📍 {m.max_chat_title}\nID MAX: {m.max_chat_id}\nID VK: {m.target_chat_id}", keyboard=kb.get_json())

@bot.on.private_message()
async def private_handler(message: Message):
    uid = message.from_id
    if message.payload:
        p = message.get_payload_json()
        if p.get("cmd") == "del":
            database.delete_chat_mapping(p.get("id"))
            await message.answer("Чат отключён")
            return

    if uid == ADMIN_ID and message.text and message.text.lower().startswith("limit "):
        try:
            _, tid, count = message.text.split()
            database.set_user_limit(int(tid), "vk", int(count))
            await message.answer(f"Лимит для {tid} установлен на {count}")
        except:
            pass
        return

    if message.text and "max.ru/join/" in message.text:
        if len(database.get_user_mappings(uid, "vk")) >= database.get_user_limit(uid, "vk"):
            await message.answer("Достигнут лимит подключённых чатов")
            return
        try:
            chat = await max_client_instance.join_chat(message.text.strip())
            code = generate_code()
            database.add_pending_connection(code, chat['id'], chat.get('title', 'Без названия'), uid, "vk")
            await message.answer(f"Я зашёл в «{chat.get('title')}». Для привязки группового чата добавь меня в этот чат во ВКонтакте, выдай доступ к переписке и введи этот код: {code}")
        except Exception as e:
            await message.answer(f"Ошибка: {e}")

@bot.on.chat_message()
async def chat_handler(message: Message):
    code = message.text.strip().upper()
    pending = database.pop_pending_connection(code)
    if pending:
        if database.add_chat_mapping(pending.max_chat_id, pending.max_chat_title, "vk", message.peer_id, pending.user_platform_id, "vk"):
            await message.answer(f"Чат «{pending.max_chat_title}» успешно привязан!")
        else:
            await message.answer("Этот чат уже привязан к этой группе")

async def send_to_vk(chat_id: int, text: str, format_data: str = "", attachments: list = None):
    """Отправляет сообщение во ВКонтакте"""
    params = {"peer_id": chat_id, "message": text, "random_id": 0}
    if format_data:
        params["format_data"] = format_data
    if attachments:
        params["attachment"] = ",".join(attachments)
    await bot.api.messages.send(**params)
