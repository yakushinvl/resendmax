import asyncio
import html
import logging
import os
import random
import time

from dotenv import load_dotenv

load_dotenv()

import database
from formatter import (
    build_tg_keyboard,
    build_vk_keyboard,
    extract_max_buttons,
    extract_max_polls,
    max_elements_to_html,
    max_elements_to_vk_format,
)
from max_client import MaxClient
import tg_bot
import vk_bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("Orchestrator")

ATTACHMENTS_DIR = "attachments"
os.makedirs(ATTACHMENTS_DIR, exist_ok=True)


async def handle_max_message(msg_payload):
    chat_id = msg_payload.get("chatId")
    msg = msg_payload.get("message", {})

    if msg.get("type") == "CONTROL":
        return

    text = msg.get("text", "")
    elements = msg.get("elements", [])

    mappings = database.get_mappings_for_max_chat(chat_id)
    if not mappings:
        return

    # Определение автора сообщения (Имя и Фамилия пользователя / название бота)
    sender_id = msg.get("sender") or msg.get("author") or msg_payload.get("sender")
    author_name = ""
    if sender_id:
        try:
            author_name = await max_client.get_user_name(sender_id)
        except Exception as se:
            logger.warning(f"Не удалось получить имя автора {sender_id}: {se}")

    # Если автор не найден (например, пост от имени канала/группы), используем название чата
    if not author_name:
        for m in mappings:
            if m.max_chat_title and m.max_chat_title != "Неизвестный чат":
                author_name = m.max_chat_title
                break
        if not author_name:
            try:
                author_name = await max_client.get_chat_title(chat_id)
            except Exception:
                pass

    vk_attachments, tg_media, downloaded_files = [], [], []
    vk_peer_id = next((m.target_chat_id for m in mappings if m.platform == "vk"), 0)

    # Захват вложений (фото, видео, файлы, аудио)
    for a in msg.get("attaches", []):
        atype = a.get("_type") or a.get("type")
        if atype in ("STICKER", "INLINE_KEYBOARD", "KEYBOARD", "POLL"):
            continue
        try:
            url, name = None, None
            is_photo = is_video = is_file = is_audio = False

            if atype == "PHOTO":
                url = a.get("baseUrl") or a.get("url") or a.get("previewUrl") or a.get("baseRawUrl")
                name = f"p_{int(time.time())}_{random.randint(100, 999)}.jpg"
                is_photo = True
            elif atype == "VIDEO":
                vid = a.get("videoId") or a.get("video_id")
                if vid:
                    try:
                        url = await max_client.get_video_url(chat_id, msg.get("id"), vid)
                    except Exception as ve:
                        logger.warning(f"Не удалось получить прямую ссылку на видео {vid}: {ve}")
                if not url:
                    url = a.get("url") or a.get("videoUrl") or a.get("downloadUrl")
                name = f"v_{int(time.time())}_{random.randint(100, 999)}.mp4"
                is_video = True
            elif atype == "FILE":
                fid = a.get("fileId") or a.get("file_id")
                if fid:
                    try:
                        url = await max_client.get_file_url(chat_id, msg.get("id"), fid)
                    except Exception as fe:
                        logger.warning(f"Не удалось получить прямую ссылку на файл {fid}: {fe}")
                if not url:
                    url = a.get("url") or a.get("downloadUrl") or a.get("fileUrl")
                name = a.get("name") or a.get("fileName") or f"f_{int(time.time())}_{random.randint(100, 999)}"
                is_file = True
            elif atype == "AUDIO":
                url = a.get("url") or a.get("audioUrl")
                name = a.get("name") or f"a_{int(time.time())}_{random.randint(100, 999)}.mp3"
                is_audio = True

            if url:
                path = os.path.join(ATTACHMENTS_DIR, name)
                data = await max_client.download_file(url)
                with open(path, "wb") as f:
                    f.write(data)
                downloaded_files.append(path)

                # Загрузка вложений для ВКонтакте
                if any(m.platform == "vk" for m in mappings) and vk_bot.bot:
                    try:
                        if is_photo and vk_bot.photo_uploader:
                            vk_attachments.append(await vk_bot.photo_uploader.upload(path, peer_id=vk_peer_id))
                        elif is_video:
                            uploaded_v = None
                            if vk_bot.user_video_uploader:
                                try:
                                    group_id = await vk_bot.get_group_id()
                                    v = await vk_bot.user_video_uploader.upload(path, name=name, group_id=group_id)
                                    uploaded_v = v if isinstance(v, str) else f"video{v.owner_id}_{v.video_id}"
                                except Exception as ue:
                                    logger.warning(f"Ошибка загрузки видео через user_video_uploader: {ue}")
                            if not uploaded_v and vk_bot.doc_uploader:
                                # Если нет user token или ошибка загрузки видео - отправляем как документ
                                uploaded_v = await vk_bot.doc_uploader.upload(path, title=name, peer_id=vk_peer_id)
                            if uploaded_v:
                                vk_attachments.append(uploaded_v)
                        elif (is_file or is_audio) and vk_bot.doc_uploader:
                            vk_attachments.append(await vk_bot.doc_uploader.upload(path, title=name, peer_id=vk_peer_id))
                    except Exception as vke:
                        logger.error(f"Ошибка загрузки вложения в VK: {vke}")

                # Подготовка вложений для Telegram
                if any(m.platform == "tg" for m in mappings) and tg_bot.bot:
                    try:
                        m_type = "photo" if is_photo else ("video" if is_video else ("audio" if is_audio else "file"))
                        tg_media.append({
                            "type": m_type,
                            "data": data,
                            "filename": name,
                        })
                    except Exception as tge:
                        logger.error(f"Ошибка подготовки медиа для Telegram: {tge}")
        except Exception as e:
            logger.error(f"Ошибка обработки вложения {atype}: {e}")

    # Извлечение инлайн-кнопок из MAX и построение клавиатур для TG и VK
    button_rows = extract_max_buttons(msg)
    tg_keyboard = build_tg_keyboard(button_rows)
    vk_keyboard = build_vk_keyboard(button_rows)

    # Извлечение опросов из MAX
    polls = extract_max_polls(msg)

    # Формирование опросов для ВКонтакте
    for p in polls:
        if any(m.platform == "vk" for m in mappings) and vk_bot.bot:
            poll_attach = await vk_bot.create_vk_poll(
                question=p["question"],
                options=p["options"],
                is_anonymous=p["is_anonymous"],
                is_multiple=p["allows_multiple_answers"],
            )
            if poll_attach:
                vk_attachments.append(poll_attach)
            else:
                poll_text = f"\n\n📊 Опрос: {p['question']}\n" + "\n".join(f"• {opt}" for opt in p["options"])
                text = (text + poll_text).strip() if text else poll_text.strip()

    # Если в сообщении нет ни текста, ни медиафайлов, ни кнопок, ни опросов - не пересылаем
    if not (text or vk_attachments or tg_media or button_rows or polls):
        return

    # Формирование текста для Telegram с указанием автора
    formatted_html = max_elements_to_html(text, elements)
    if author_name:
        if formatted_html:
            tg_text = f"<b>{html.escape(author_name)}:</b>\n{formatted_html}"
        else:
            tg_text = f"<b>{html.escape(author_name)}</b>"
    else:
        tg_text = formatted_html

    # Формирование текста для ВКонтакте с указанием автора и подсветкой
    if author_name:
        if text:
            vk_prefix = f"{author_name}:\n"
            vk_text = f"{vk_prefix}{text}"
            vk_format = max_elements_to_vk_format(
                elements,
                offset_shift=len(vk_prefix),
                author_len=len(author_name),
            )
        else:
            vk_text = author_name
            vk_format = max_elements_to_vk_format(
                [],
                offset_shift=0,
                author_len=len(author_name),
            )
    else:
        vk_text = text
        vk_format = max_elements_to_vk_format(elements)

    # Рассылка по целевым чатам
    for m in mappings:
        try:
            if m.platform == "vk" and (vk_text or vk_attachments or vk_keyboard):
                await vk_bot.send_to_vk(
                    m.target_chat_id,
                    vk_text,
                    format_data=vk_format,
                    attachments=vk_attachments,
                    keyboard=vk_keyboard,
                )
            elif m.platform == "tg":
                # Если есть текст, медиавложения или кнопки (без опросов) - отправляем их
                if tg_text or tg_media or (tg_keyboard and not polls):
                    await tg_bot.send_to_tg(
                        m.target_chat_id,
                        tg_text,
                        media=tg_media,
                        thread_id=m.target_thread_id,
                        reply_markup=tg_keyboard if not polls else None,
                    )
                # Отправляем опросы в Telegram
                for p in polls:
                    poll_question = f"{author_name}: {p['question']}" if (author_name and not text) else p["question"]
                    await tg_bot.send_poll_to_tg(
                        chat_id=m.target_chat_id,
                        question=poll_question,
                        options=p["options"],
                        is_anonymous=p["is_anonymous"],
                        allows_multiple_answers=p["allows_multiple_answers"],
                        is_closed=p["is_closed"],
                        thread_id=m.target_thread_id,
                        reply_markup=tg_keyboard,
                    )
        except Exception as e:
            logger.error(f"Ошибка пересылки в {m.platform}: {e}")

    # Очистка временных файлов
    for p in downloaded_files:
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


async def main():
    database.create_db_and_tables()
    m_tok, m_phone = os.getenv("MAX_TOKEN"), os.getenv("MAX_PHONE")
    if not m_tok and not m_phone:
        logger.error("Не указан MAX_TOKEN или MAX_PHONE в конфигурации .env")
        return

    global max_client
    max_client = MaxClient(token=m_tok, phone=m_phone)
    vk_bot.max_client_instance = tg_bot.max_client_instance = max_client

    try:
        await max_client.connect()
    except Exception as e:
        logger.error(f"Не удалось подключиться к MAX: {e}")
        return

    max_client.add_message_handler(handle_max_message)
    tasks = []
    if os.getenv("VK_TOKEN") and vk_bot.bot:
        vk_bot.bot.loop_wrapper.loop = asyncio.get_running_loop()
        vk_bot.bot.loop_wrapper._running = True
        tasks.append(vk_bot.bot.run_polling())
        logger.info("Бот ВКонтакте готов")

    if os.getenv("TG_TOKEN") and tg_bot.bot:
        tasks.append(tg_bot.dp.start_polling(tg_bot.bot))
        logger.info("Бот Telegram готов")

    if tasks:
        logger.info("Запуск ботов...")
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
