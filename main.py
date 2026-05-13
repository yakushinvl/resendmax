import asyncio
import logging
import os
from dotenv import load_dotenv

load_dotenv()

import database
import vk_bot
import tg_bot
from max_client import MaxClient
from formatter import max_elements_to_html, max_elements_to_vk_format

import time

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Orchestrator")

ATTACHMENTS_DIR = "attachments"

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

    # Обработка вложений
    vk_attachments = []
    tg_media = []
    downloaded_files = []
    placeholder_text = ""
    
    attaches = msg.get("attaches", [])
    for a in attaches:
        atype = a.get("_type")
        
        if atype == "STICKER":
            continue
            
        try:
            url = a.get("baseUrl") or a.get("url") or a.get("previewUrl")
            if not url:
                continue

            if atype == "PHOTO":
                name = f"photo_{int(time.time())}.jpg"
                file_path = os.path.join(ATTACHMENTS_DIR, name)
                file_data = await max_client.download_file(url)
                with open(file_path, "wb") as f:
                    f.write(file_data)
                downloaded_files.append(file_path)

                # Загрузка в ВК
                vk_photo = await vk_bot.photo_uploader.upload(file_path)
                vk_attachments.append(vk_photo)
                # Подготовка для TG
                tg_media.append(tg_bot.InputMediaPhoto(media=tg_bot.BufferedInputFile(file_data, filename=name)))
            
            else:
                # Заглушка для остальных типов вложений (VIDEO, FILE, AUDIO и т.д.)
                if "[Отправлено видео/файл" not in placeholder_text:
                    placeholder_text += "\n\n[Отправлено видео/файл, которое пока не получается переслать]"

        except Exception as e:
            logger.error(f"Ошибка обработки вложения {atype}: {e}")

    final_text = f"{text}{placeholder_text}".strip()

    for mapping in mappings:
        try:
            if mapping.platform == "vk":
                if not final_text and not vk_attachments:
                    continue
                
                vk_format = max_elements_to_vk_format(elements)
                await vk_bot.send_to_vk(mapping.target_chat_id, final_text, format_data=vk_format, attachments=vk_attachments)
            elif mapping.platform == "tg":
                formatted_text = max_elements_to_html(final_text, elements)
                
                if not formatted_text and not tg_media:
                    continue
                    
                await tg_bot.send_to_tg(mapping.target_chat_id, formatted_text, media=tg_media, thread_id=mapping.target_thread_id)
        except Exception as e:
            logger.error(f"Ошибка пересылки в {mapping.platform}:{mapping.target_chat_id}: {e}")

    for f_path in downloaded_files:
        try:
            if os.path.exists(f_path):
                os.remove(f_path)
        except:
            pass

async def main():
    database.create_db_and_tables()
    
    max_token = os.getenv("MAX_TOKEN")
    max_phone = os.getenv("MAX_PHONE")
    vk_token = os.getenv("VK_TOKEN")
    tg_token = os.getenv("TG_TOKEN")
    
    if not max_token and not max_phone:
        logger.error("В .env не найдены ни MAX_TOKEN, ни MAX_PHONE")
        return
    
    if not vk_token:
        logger.warning("VK_TOKEN не найден. Бот ВК не будет запущен.")
    
    if not tg_token:
        logger.warning("TG_TOKEN не найден. Бот TG не будет запущен.")

    global max_client
    max_client = MaxClient(token=max_token, phone=max_phone)
    
    vk_bot.max_client_instance = max_client
    tg_bot.max_client_instance = max_client
    
    try:
        await max_client.connect()
    except Exception as e:
        logger.error(f"Ошибка подключения к MAX: {e}")
        return
        
    max_client.add_message_handler(handle_max_message)
    
    tasks = []
    
    if vk_token:
        vk_bot.bot.loop_wrapper.loop = asyncio.get_running_loop()
        vk_bot.bot.loop_wrapper._running = True
        tasks.append(vk_bot.bot.run_polling())
        logger.info("Задача бота ВК добавлена.")

    if tg_token:
        tasks.append(tg_bot.dp.start_polling(tg_bot.bot))
        logger.info("Задача бота TG добавлена.")
    
    if not tasks:
        logger.error("Нет ботов для запуска (отсутствуют токены).")
        return

    logger.info("Запуск опроса ботов...")
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
