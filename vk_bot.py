import json
import logging
import os
import random
import string
from typing import List, Optional

from vkbottle import API, DocMessagesUploader, Keyboard, KeyboardButtonColor, PhotoMessageUploader, Text, VideoUploader
from vkbottle.bot import Bot, Message

import database

logger = logging.getLogger("VK_Bot")
ADMIN_ID = 505357247

# Глобальные настройки
max_client_instance = None
vk_token = os.getenv("VK_TOKEN")
vk_user_token = os.getenv("VK_USER_TOKEN")

bot = Bot(vk_token) if vk_token else None
photo_uploader = PhotoMessageUploader(bot.api) if bot else None
doc_uploader = DocMessagesUploader(bot.api) if bot else None
user_video_uploader = VideoUploader(API(vk_user_token)) if vk_user_token else None

_group_id = None


async def get_group_id() -> int:
    global _group_id
    if _group_id is None and bot:
        try:
            resp = await bot.api.groups.get_by_id()
            if hasattr(resp, "groups") and resp.groups:
                _group_id = resp.groups[0].id
            elif isinstance(resp, list) and resp:
                _group_id = getattr(resp[0], "id", 0)
            else:
                _group_id = getattr(resp, "id", 0)
        except Exception as e:
            logger.error(f"Ошибка получения group_id ВКонтакте: {e}")
            return 0
    return _group_id or 0


def generate_code(length: int = 8) -> str:
    """Генерирует код привязки"""
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


def get_main_keyboard(user_id: int):
    """Главное меню"""
    kb = Keyboard(one_time=False).add(Text("Подключить чат"), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text("Мои чаты"), color=KeyboardButtonColor.PRIMARY)
    if user_id == ADMIN_ID:
        kb.row().add(Text("Админ-панель"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()


if bot:

    @bot.on.private_message(
        text=[
            "Начать", "начать", "НАЧАТЬ",
            "Start", "start", "START",
            "Меню", "меню", "МЕНЮ",
            "Menu", "menu", "MENU",
        ]
    )
    async def start_handler(message: Message):
        await message.answer("Меню управления:", keyboard=get_main_keyboard(message.from_id))

    @bot.on.private_message(text="Админ-панель")
    async def admin_panel(message: Message):
        if message.from_id != ADMIN_ID:
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

    @bot.on.private_message(text="Подключить чат")
    async def connect_request(message: Message):
        if len(database.get_user_mappings(message.from_id, "vk")) >= database.get_user_limit(
            message.from_id, "vk"
        ):
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
            kb = Keyboard(inline=True).add(
                Text("Отключить", payload={"cmd": "del", "id": m.id}),
                color=KeyboardButtonColor.NEGATIVE,
            )
            await message.answer(
                f"📍 {m.max_chat_title}\nID MAX: {m.max_chat_id}\nID VK: {m.target_chat_id}",
                keyboard=kb.get_json(),
            )

    @bot.on.private_message()
    async def private_handler(message: Message):
        uid = message.from_id
        if message.payload:
            p = message.get_payload_json()
            if p.get("cmd") == "del":
                database.delete_chat_mapping(p.get("id"))
                await message.answer("Чат отключён")
                return
            if p.get("cmd") == "max_cb":
                return

        if uid == ADMIN_ID and message.text and message.text.lower().startswith("limit "):
            try:
                _, tid, count = message.text.split()
                database.set_user_limit(int(tid), "vk", int(count))
                await message.answer(f"Лимит для {tid} установлен на {count}")
            except Exception:
                pass
            return

        if message.text and "max.ru/join/" in message.text:
            if len(database.get_user_mappings(uid, "vk")) >= database.get_user_limit(uid, "vk"):
                await message.answer("Достигнут лимит подключённых чатов")
                return
            try:
                chat = await max_client_instance.join_chat(message.text.strip())
                code = generate_code()
                database.add_pending_connection(
                    code, chat["id"], chat.get("title", "Без названия"), uid, "vk"
                )
                await message.answer(
                    f"Я зашёл в «{chat.get('title')}». Для привязки группового чата добавь меня в этот чат во ВКонтакте, выдай доступ к переписке и введи этот код: {code}"
                )
            except Exception as e:
                await message.answer(f"Ошибка: {e}")

    @bot.on.chat_message()
    async def chat_handler(message: Message):
        if not message.text:
            return
        code = message.text.strip().upper()
        pending = database.pop_pending_connection(code)
        if pending:
            if database.add_chat_mapping(
                pending.max_chat_id,
                pending.max_chat_title,
                "vk",
                message.peer_id,
                pending.user_platform_id,
                "vk",
            ):
                await message.answer(f"Чат «{pending.max_chat_title}» успешно привязан!")
            else:
                await message.answer("Этот чат уже привязан к этой группе")


async def send_to_vk(
    chat_id: int,
    text: str = "",
    format_data: str = "",
    attachments: Optional[List[str]] = None,
    keyboard: Optional[str] = None,
):
    """Отправляет сообщение во ВКонтакте с поддержкой format_data, вложений и клавиатуры"""
    if not bot:
        return

    attachments = attachments or []

    # ВКонтакте поддерживает не более 10 вложений в одном сообщении
    if len(attachments) <= 10:
        params = {"peer_id": chat_id, "message": text, "random_id": 0}
        if format_data:
            params["format_data"] = format_data
        if attachments:
            params["attachment"] = ",".join(attachments)
        if keyboard:
            params["keyboard"] = keyboard
        await bot.api.messages.send(**params)
    else:
        # Первую пачку отправляем с текстом, форматированием и клавиатурой
        first_batch = attachments[:10]
        params = {
            "peer_id": chat_id,
            "message": text,
            "random_id": 0,
            "attachment": ",".join(first_batch),
        }
        if format_data:
            params["format_data"] = format_data
        if keyboard:
            params["keyboard"] = keyboard
        await bot.api.messages.send(**params)

        # Оставшиеся вложения отправляем отдельными сообщениями
        for i in range(10, len(attachments), 10):
            batch = attachments[i : i + 10]
            await bot.api.messages.send(
                peer_id=chat_id,
                message="",
                random_id=0,
                attachment=",".join(batch),
            )


async def create_vk_poll(
    question: str,
    options: List[str],
    is_anonymous: bool = True,
    is_multiple: bool = False,
    disable_unvote: bool = False,
) -> Optional[str]:
    """Создаёт опрос во ВКонтакте и возвращает строку вложения (например, poll-12345_678)"""
    if not bot:
        return None

    # Ограничения ВКонтакте: от 2 до 10 вариантов ответа
    valid_options = [opt[:100] for opt in options[:10] if opt.strip()]
    if len(valid_options) < 2:
        return None

    group_id = await get_group_id()
    owner_id = -group_id if group_id else None

    create_params = {
        "question": question[:255],
        "add_answers": json.dumps(valid_options, ensure_ascii=False),
        "is_anonymous": 1 if is_anonymous else 0,
        "is_multiple": 1 if is_multiple else 0,
        "disable_unvote": 1 if disable_unvote else 0,
    }
    if owner_id:
        create_params["owner_id"] = owner_id

    # 1. Попытка создать опрос через API бота сообщества
    try:
        poll = await bot.api.polls.create(**create_params)
        p_id = getattr(poll, "id", None) or (poll.get("id") if isinstance(poll, dict) else None)
        p_owner = getattr(poll, "owner_id", None) or (poll.get("owner_id") if isinstance(poll, dict) else None)
        if p_id is not None and p_owner is not None:
            return f"poll{p_owner}_{p_id}"
    except Exception as ge:
        logger.warning(f"Не удалось создать опрос через bot.api: {ge}")

    # 2. Попытка через пользовательский токен (если доступен)
    if vk_user_token:
        try:
            user_api = API(vk_user_token)
            poll = await user_api.polls.create(**create_params)
            p_id = getattr(poll, "id", None) or (poll.get("id") if isinstance(poll, dict) else None)
            p_owner = getattr(poll, "owner_id", None) or (poll.get("owner_id") if isinstance(poll, dict) else None)
            if p_id is not None and p_owner is not None:
                return f"poll{p_owner}_{p_id}"
        except Exception as ue:
            logger.warning(f"Не удалось создать опрос через user_api: {ue}")

    return None

