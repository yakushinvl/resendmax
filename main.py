import asyncio
import logging
import os
import time
import random

from dotenv import load_dotenv
load_dotenv()

import database
import vk_bot
import tg_bot
from max_client import MaxClient
from formatter import max_elements_to_html, max_elements_to_vk_format

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
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

    vk_attachments, tg_media, downloaded_files = [], [], []
    vk_peer_id = next((m.target_chat_id for m in mappings if m.platform == "vk"), 0)

    for a in msg.get("attaches", []):
        atype = a.get("_type")
        if atype == "STICKER":
            continue
        try:
            url, name = None, None
            is_photo = is_video = is_file = False

            if atype == "PHOTO":
                url = a.get("baseUrl") or a.get("url") or a.get("previewUrl")
                name = f"p_{int(time.time())}_{random.randint(100,999)}.jpg"
                is_photo = True
            elif atype == "VIDEO":
                vid = a.get("videoId")
                if vid:
                    url = await max_client.get_video_url(chat_id, msg.get("id"), vid)
                    name = f"v_{int(time.time())}_{random.randint(100,999)}.mp4"
                    is_video = True
            elif atype == "FILE":
                fid = a.get("fileId")
                if fid:
                    url = await max_client.get_file_url(chat_id, msg.get("id"), fid)
                    name = a.get("name") or f"f_{int(time.time())}_{random.randint(100,999)}"
                    is_file = True

            if url:
                path = os.path.join(ATTACHMENTS_DIR, name)
                data = await max_client.download_file(url)
                with open(path, "wb") as f:
                    f.write(data)
                downloaded_files.append(path)

                if is_photo:
                    vk_attachments.append(await vk_bot.photo_uploader.upload(path, peer_id=vk_peer_id))
                    tg_media.append(tg_bot.InputMediaPhoto(media=tg_bot.BufferedInputFile(data, filename=name)))
                elif is_video:
                    v = await (vk_bot.user_video_uploader or vk_bot.video_uploader).upload(path, name=name, group_id=await vk_bot.get_group_id())
                    vk_attachments.append(v if isinstance(v, str) else f"video{v.owner_id}_{v.video_id}")
                    tg_media.append(tg_bot.InputMediaVideo(media=tg_bot.BufferedInputFile(data, filename=name)))
                elif is_file:
                    vk_attachments.append(await vk_bot.doc_uploader.upload(path, title=name, peer_id=vk_peer_id))
                    tg_media.append(tg_bot.InputMediaDocument(media=tg_bot.BufferedInputFile(data, filename=name)))
        except Exception as e:
            logger.error(f"Ошибка с аттачем {atype}: {e}")

    for m in mappings:
        try:
            if m.platform == "vk" and (text or vk_attachments):
                await vk_bot.send_to_vk(m.target_chat_id, text, format_data=max_elements_to_vk_format(elements), attachments=vk_attachments)
            elif m.platform == "tg" and (text or tg_media):
                await tg_bot.send_to_tg(m.target_chat_id, max_elements_to_html(text, elements), media=tg_media, thread_id=m.target_thread_id)
        except Exception as e:
            logger.error(f"Ошибка пересылки {m.platform}: {e}")

    for p in downloaded_files:
        try: 
            if os.path.exists(p):
                os.remove(p)
        except:
            pass

async def main():
    database.create_db_and_tables()
    m_tok, m_phone = os.getenv("MAX_TOKEN"), os.getenv("MAX_PHONE")
    if not m_tok and not m_phone: return
    
    global max_client
    max_client = MaxClient(token=m_tok, phone=m_phone)
    vk_bot.max_client_instance = tg_bot.max_client_instance = max_client
    
    try:
        await max_client.connect()
    except:
        return
        
    max_client.add_message_handler(handle_max_message)
    tasks = []
    if os.getenv("VK_TOKEN"):
        vk_bot.bot.loop_wrapper.loop = asyncio.get_running_loop()
        vk_bot.bot.loop_wrapper._running = True
        tasks.append(vk_bot.bot.run_polling())
        logger.info("Бот ВКонтакте готов")

    if os.getenv("TG_TOKEN"):
        tasks.append(tg_bot.dp.start_polling(tg_bot.bot))
        logger.info("Бот Telegram готов")
    
    if tasks:
        logger.info("Запуск ботов")
        await asyncio.gather(*tasks)

try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
