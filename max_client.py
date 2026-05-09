import asyncio
import json
import logging
import time
import ssl
import uuid
import sys
import aiohttp
import websockets
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

# Константы
WEBSOCKET_URI = "wss://ws-api.oneme.ru/websocket"
HOST = "api.oneme.ru"
PORT = 443
WEBSOCKET_ORIGIN = "https://web.max.ru"
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

class Opcode(int, Enum):
    PING = 1
    SESSION_INIT = 6
    AUTH_REQUEST = 17
    AUTH = 18
    LOGIN = 19
    CHAT_INFO = 48
    CHAT_MARK = 50
    CHATS_LIST = 53
    CHAT_JOIN = 57
    CHAT_LEAVE = 58
    MSG_SEND = 64
    MSG_DELETE = 66
    MSG_EDIT = 67
    NOTIF_MESSAGE = 128
    NOTIF_CHAT = 135

class MaxClient:
    def __init__(self, token: Optional[str] = None, phone: Optional[str] = None, logger: Optional[logging.Logger] = None):
        self.token, self.phone = token, phone
        self.logger = logger or logging.getLogger("MaxClient")
        self.ws, self.seq = None, 0
        self.device_id = str(uuid.uuid4())
        self.pending_responses: Dict[int, asyncio.Future] = {}
        self.message_handlers: List[Callable] = []
        self.is_connected = False

    async def connect(self):
        """Подключается к WebSocket и выполняет вход"""
        ssl_context = ssl.create_default_context()
        self.ws = await websockets.connect(WEBSOCKET_URI, ssl=ssl_context, origin=WEBSOCKET_ORIGIN, user_agent_header=DEFAULT_USER_AGENT)
        self.is_connected = True
        asyncio.create_task(self._recv_loop())
        
        await self._send_and_wait(Opcode.SESSION_INIT, {"deviceId": self.device_id, "userAgent": {"deviceType": "WEB"}})
        if not self.token and self.phone: await self._login_flow()
        if not self.token: raise ValueError("Для MaxClient не указан ни токен, ни телефон")

        login_resp = await self._send_and_wait(Opcode.LOGIN, {
            "token": self.token, "interactive": True, "chatsSync": 0,
            "contactsSync": 0, "presenceSync": -1, "userAgent": {"deviceType": "WEB"}
        })
        profile = login_resp.get("payload", {}).get("profile", {}).get("contact", {})
        self.logger.info(f"Вход выполнен: {profile.get('names', [{}])[0].get('name')} ({profile.get('id')})")
        asyncio.create_task(self._ping_loop())

    async def _login_flow(self):
        """Процесс входа по номеру телефона через консоль"""
        temp_token = await self.request_code(self.phone)
        print(f"Введите код подтверждения для {self.phone}: ", end="", flush=True)
        code = (await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)).strip()
        login_resp = await self.send_code(code, temp_token)
        self.token = login_resp.get("tokenAttrs", {}).get("LOGIN", {}).get("token")
        if not self.token: raise Exception("Не удалось получить токен авторизации")

    def _split_phone(self, phone: str) -> str:
        if phone.startswith("+7"):
            p = phone[2:]
            return f"+7 {p[:3]} {p[3:6]} {p[6:8]} {p[8:]}"
        return phone

    async def request_code(self, phone: str) -> str:
        resp = await self._send_and_wait(Opcode.AUTH_REQUEST, {"phone": self._split_phone(phone), "type": "START_AUTH"})
        if resp.get("payload", {}).get("error"): raise Exception(f"Ошибка запроса кода: {resp['payload']['error']}")
        return resp.get("payload", {}).get("token")

    async def send_code(self, code: str, token: str) -> Dict:
        resp = await self._send_and_wait(Opcode.AUTH, {"token": token, "verifyCode": code, "authToken_type": "CHECK_CODE"})
        if resp.get("payload", {}).get("error"): raise Exception(f"Ошибка проверки кода: {resp['payload']['error']}")
        return resp.get("payload")

    async def download_file(self, url: str) -> bytes:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200: raise Exception(f"Ошибка скачивания: {resp.status}")
                return await resp.read()

    async def _ping_loop(self):
        while self.is_connected:
            try:
                await self._send_and_wait(Opcode.PING, {"interactive": True})
                await asyncio.sleep(30)
            except: break

    async def _recv_loop(self):
        try:
            async for message in self.ws:
                data = json.loads(message)
                seq, opcode = data.get("seq"), data.get("opcode")
                if seq in self.pending_responses: self.pending_responses[seq].set_result(data)
                if opcode == Opcode.NOTIF_MESSAGE:
                    for handler in self.message_handlers: asyncio.create_task(handler(data.get("payload", {})))
        except: self.is_connected = False

    async def _send_and_wait(self, opcode: Opcode, payload: Dict, timeout: float = 10.0) -> Dict:
        seq = self.seq
        self.seq += 1
        future = asyncio.get_event_loop().create_future()
        self.pending_responses[seq] = future
        await self.ws.send(json.dumps({"ver": 11, "cmd": 0, "seq": seq, "opcode": int(opcode), "payload": payload}))
        try: return await asyncio.wait_for(future, timeout=timeout)
        finally: self.pending_responses.pop(seq, None)

    async def join_chat(self, link: str) -> Dict:
        idx = link.find("join/")
        if idx == -1: raise ValueError("Неверная ссылка")
        resp = await self._send_and_wait(Opcode.CHAT_JOIN, {"link": link[idx:]})
        if resp.get("payload", {}).get("error"): raise Exception(f"Ошибка входа: {resp['payload']['error']}")
        return resp.get("payload", {}).get("chat")

    async def send_message(self, chat_id: int, text: str):
        payload = {"chatId": chat_id, "message": {"text": text, "cid": int(time.time() * 1000)}, "notify": True}
        await self._send_and_wait(Opcode.MSG_SEND, payload)

    def add_message_handler(self, handler: Callable):
        self.message_handlers.append(handler)
