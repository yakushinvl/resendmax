import asyncio
import json
import logging
import ssl
import uuid
import sys
import aiohttp
import websockets
from enum import Enum
from typing import Callable, Dict, List, Optional

# Константы
WEBSOCKET_URI = "wss://ws-api.oneme.ru/websocket"
WEBSOCKET_ORIGIN = "https://web.max.ru"
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


class Opcode(int, Enum):
    PING = 1
    SESSION_INIT = 6
    AUTH_REQUEST = 17
    AUTH = 18
    LOGIN = 19
    CHAT_MARK = 50
    CHAT_JOIN = 57
    MSG_SEND = 64
    VIDEO_PLAY = 83
    FILE_DOWNLOAD = 88
    NOTIF_MESSAGE = 128


class MaxClient:
    def __init__(self, token: Optional[str] = None, phone: Optional[str] = None, logger: Optional[logging.Logger] = None):
        self.token, self.phone = token, phone
        self.logger = logger or logging.getLogger("MaxClient")
        self.ws, self.seq = None, 0
        self.device_id = str(uuid.uuid4())
        self.pending_responses: Dict[int, asyncio.Future] = {}
        self.message_handlers: List[Callable] = []
        self.is_connected = False
        self._ping_task = None

    async def connect(self):
        """Подключается к WebSocket и выполняет вход"""
        if self.is_connected: return

        ssl_context = ssl.create_default_context()
        try:
            self.ws = await websockets.connect(WEBSOCKET_URI, ssl=ssl_context, origin=WEBSOCKET_ORIGIN, user_agent_header=DEFAULT_USER_AGENT)
            self.is_connected = True

            # Очистка ожидающих ответов
            for f in list(self.pending_responses.values()):
                if not f.done(): f.set_exception(Exception("Connection reset"))
            self.pending_responses.clear()

            asyncio.create_task(self._recv_loop())

            await self._send_and_wait(Opcode.SESSION_INIT, {"deviceId": self.device_id, "userAgent": {"deviceType": "WEB"}})
            if not self.token and self.phone: await self._login_flow()
            if not self.token: raise ValueError("Отсутствует токен или номер телефона")

            login_resp = await self._send_and_wait(Opcode.LOGIN, {
                "token": self.token,
                "chatsCount": 40,
                "interactive": True,
                "chatsSync": -1,
                "contactsSync": -1,
                "presenceSync": -1,
                "draftsSync": -1
            })
            if login_resp.get("payload", {}).get("error"):
                raise Exception(f"Ошибка входа: {login_resp['payload']['error']}")

            new_token = login_resp.get("payload", {}).get("token")
            if new_token:
                self.token = new_token

            self.logger.info(f"Вход выполнен")

            if not self._ping_task or self._ping_task.done():
                self._ping_task = asyncio.create_task(self._ping_loop())
        except Exception as e:
            self.is_connected = False
            self.logger.error(f"Ошибка подключения: {e}")
            raise

    async def _login_flow(self):
        """Вход по номеру телефона через консоль"""
        temp_token = await self.request_code(self.phone)
        print(f"Введите код для {self.phone}: ", end="", flush=True)
        code = (await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)).strip()
        resp = await self.send_code(code, temp_token)
        self.token = resp.get("tokenAttrs", {}).get("LOGIN", {}).get("token")
        if not self.token: raise Exception("Не удалось получить токен")

    def _format_phone(self, phone: str) -> str:
        if phone.startswith("+7") and len(phone) == 12:
            return f"+7 {phone[2:5]} {phone[5:8]} {phone[8:10]} {phone[10:]}"
        return phone

    async def request_code(self, phone: str) -> str:
        resp = await self._send_and_wait(Opcode.AUTH_REQUEST, {"phone": self._format_phone(phone), "type": "START_AUTH"})
        if resp.get("payload", {}).get("error"): raise Exception(resp['payload']['error'])
        return resp.get("payload", {}).get("token")

    async def send_code(self, code: str, token: str) -> Dict:
        resp = await self._send_and_wait(Opcode.AUTH, {"token": token, "verifyCode": code, "authToken_type": "CHECK_CODE"})
        if resp.get("payload", {}).get("error"): raise Exception(resp['payload']['error'])
        return resp.get("payload")

    async def download_file(self, url: str) -> bytes:
        async with aiohttp.ClientSession() as s:
            async with s.get(url) as r:
                return await r.read()

    async def get_video_url(self, chat_id: int, message_id: int, video_id: int) -> str:
        resp = await self._send_and_wait(Opcode.VIDEO_PLAY, {"chatId": chat_id, "messageId": message_id, "videoId": video_id})
        p = resp.get("payload", {})
        for k, v in p.items():
            if k not in ("EXTERNAL", "cache") and isinstance(v, str) and v.startswith("http"): return v
        raise Exception("URL видео не найден")

    async def get_file_url(self, chat_id: int, message_id: int, file_id: int) -> str:
        resp = await self._send_and_wait(Opcode.FILE_DOWNLOAD, {"chatId": chat_id, "messageId": message_id, "fileId": file_id})
        url = resp.get("payload", {}).get("url")
        if not url: raise Exception("URL файла не найден")
        return url

    async def _ping_loop(self):
        while True:
            if self.is_connected:
                try:
                    await self._send_and_wait(Opcode.PING, {"interactive": True}, timeout=5.0)
                except:
                    self.is_connected = False
            else:
                try:
                    await self.connect()
                except:
                    await asyncio.sleep(5)
            await asyncio.sleep(30)

    async def _recv_loop(self):
        try:
            async for msg in self.ws:
                data = json.loads(msg)
                seq, opcode = data.get("seq"), data.get("opcode")
                if seq in self.pending_responses:
                    f = self.pending_responses.get(seq)
                    if f and not f.done(): f.set_result(data)
                if opcode == Opcode.NOTIF_MESSAGE:
                    for h in self.message_handlers: asyncio.create_task(h(data.get("payload", {})))
        except:
            self.is_connected = False

    async def _send_and_wait(self, opcode: Opcode, payload: Dict, timeout: float = 10.0) -> Dict:
        if not self.is_connected and opcode not in (Opcode.SESSION_INIT, Opcode.LOGIN, Opcode.AUTH, Opcode.AUTH_REQUEST):
            await self.connect()
        seq = self.seq
        self.seq += 1
        f = asyncio.get_event_loop().create_future()
        self.pending_responses[seq] = f
        try:
            await self.ws.send(json.dumps({"ver": 11, "cmd": 0, "seq": seq, "opcode": int(opcode), "payload": payload}))
            return await asyncio.wait_for(f, timeout=timeout)
        finally:
            self.pending_responses.pop(seq, None)

    async def join_chat(self, link: str) -> Dict:
        idx = link.find("join/")
        if idx == -1:
            raise ValueError("Неверная ссылка")
        resp = await self._send_and_wait(Opcode.CHAT_JOIN, {"link": link[idx:]})
        if resp.get("payload", {}).get("error"):
            raise Exception(resp['payload']['error'])
        return resp.get("payload", {}).get("chat")

    def add_message_handler(self, handler: Callable):
        self.message_handlers.append(handler)
