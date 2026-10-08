import asyncio
import json
import logging
import os
import random
import sqlite3
import ssl
import struct
import sys
import time
import uuid
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union

import aiohttp
import msgpack
import websockets

try:
    import zstandard
except ImportError:
    zstandard = None

# Константы подключения
WEBSOCKET_URI = "wss://api.oneme.ru/websocket"
WEBSOCKET_ORIGIN = "https://web.max.ru"
PROTOCOL_VERSION = 10
REQUEST_TIMEOUT = 30.0
MAX_FRAME_SIZE = 10 * 1024 * 1024
SESSION_FILE = "max_session.json"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
WEB_APP_VERSION = "26.7.15"
WEB_SCREEN = "1080x1920 1.0x"
LOCALE_TIMEZONES = (
    ("ru", "Europe/Moscow"), ("ru", "Europe/Kaliningrad"), ("ru", "Europe/Samara"),
    ("ru", "Asia/Yekaterinburg"), ("ru", "Asia/Omsk"), ("ru", "Asia/Novosibirsk"),
    ("ru", "Asia/Krasnoyarsk"), ("ru", "Asia/Irkutsk"), ("ru", "Asia/Yakutsk"),
    ("ru", "Asia/Vladivostok"),
)

FRAME_HEADER = struct.Struct(">BBHHI")
INVALID_TOKEN_ERRORS = ("FAIL_LOGIN_TOKEN", "FAIL_LOGOUT_ALL")
DEFAULT_CONFIG_HASH = (
    "00000000-0000000000000000-00000000-"
    "0000000000000000-0000000000000000-0-"
    "0000000000000000-00000000"
)


class Opcode(int, Enum):
    PING = 1
    DEBUG = 2
    RECONNECT = 3
    LOG = 5
    SESSION_INIT = 6
    LOGIN2 = 8
    PROFILE = 16
    AUTH_REQUEST = 17
    AUTH = 18
    LOGIN = 19
    LOGOUT = 20
    SYNC = 21
    CONFIG = 22
    AUTH_CONFIRM = 23
    CONTACT_INFO = 32
    CONTACT_ADD = 33
    CONTACT_UPDATE = 34
    CONTACT_PRESENCE = 35
    CONTACT_LIST = 36
    CONTACT_SEARCH = 37
    CONTACT_INFO_BY_PHONE = 46
    CHAT_INFO = 48
    CHAT_HISTORY = 49
    CHAT_MARK = 50
    CHAT_DELETE = 52
    CHATS_LIST = 53
    CHAT_CLEAR = 54
    CHAT_UPDATE = 55
    CHAT_CHECK_LINK = 56
    CHAT_JOIN = 57
    CHAT_LEAVE = 58
    CHAT_MEMBERS = 59
    CHAT_CREATE = 63
    MSG_SEND = 64
    MSG_TYPING = 65
    MSG_DELETE = 66
    MSG_EDIT = 67
    MSG_GET = 71
    PHOTO_UPLOAD = 80
    STICKER_UPLOAD = 81
    VIDEO_UPLOAD = 82
    VIDEO_PLAY = 83
    FILE_UPLOAD = 87
    FILE_DOWNLOAD = 88
    AUTH_CHECK_PASSWORD = 115
    NOTIF_MESSAGE = 128


class Command(int, Enum):
    REQUEST = 0
    RESPONSE = 1
    EVENT = 2
    ERROR = 3


class MaxApiError(Exception):
    """Ошибка, возвращённая сервером MAX"""

    def __init__(self, opcode: int, payload: Optional[Dict] = None):
        self.opcode = opcode
        self.payload = payload or {}
        self.error = self.payload.get("error")
        super().__init__(
            self.payload.get("localizedMessage")
            or self.payload.get("message")
            or self.error
            or "Неизвестная ошибка"
        )


def lz4_decompress(src: bytes, max_output: int = 5 * 1024 * 1024) -> bytes:
    """Распаковывает блок LZ4 без внешних зависимостей"""
    dst = bytearray()
    pos = 0

    while pos < len(src):
        token = src[pos]
        pos += 1

        lit_len = token >> 4
        if lit_len == 15:
            while pos < len(src):
                b = src[pos]
                pos += 1
                lit_len += b
                if b != 255:
                    break

        if lit_len > 0:
            if pos + lit_len > len(src):
                raise ValueError("LZ4: длина литерала за пределами блока")
            dst.extend(src[pos:pos + lit_len])
            pos += lit_len
            if len(dst) > max_output:
                raise ValueError("LZ4: слишком большой результат")

        if pos >= len(src):
            break
        if pos + 1 >= len(src):
            raise ValueError("LZ4: неполное смещение")

        offset = src[pos] | (src[pos + 1] << 8)
        pos += 2
        if offset == 0:
            raise ValueError("LZ4: нулевое смещение")

        match_len = (token & 0x0F) + 4
        if (token & 0x0F) == 0x0F:
            while pos < len(src):
                b = src[pos]
                pos += 1
                match_len += b
                if b != 255:
                    break

        match_pos = len(dst) - offset
        if match_pos < 0:
            raise ValueError("LZ4: ссылка за пределами блока")

        for i in range(match_len):
            dst.append(dst[match_pos + (i % offset)])

        if len(dst) > max_output:
            raise ValueError("LZ4: слишком большой результат")

    return bytes(dst)


def _msgpack_ext_hook(code: int, data: bytes) -> Any:
    """Разворачивает вложенные msgpack-значения (ext-код 1)"""
    if code != 1:
        return msgpack.ExtType(code, data)
    return msgpack.unpackb(data, raw=False, strict_map_key=False, ext_hook=_msgpack_ext_hook)


def _normalize_key(key: Any) -> Any:
    if isinstance(key, int):
        return str(key)
    if isinstance(key, bytes):
        try:
            return key.decode("utf-8")
        except UnicodeDecodeError:
            return key.hex()
    return key


def _normalize(obj: Any) -> Any:
    """Приводит ключи распакованных словарей к строкам"""
    if isinstance(obj, dict):
        return {_normalize_key(k): _normalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(item) for item in obj]
    return obj


def pack_frame(cmd: int, seq: int, opcode: int, payload: Optional[Dict]) -> bytes:
    """Собирает бинарный кадр протокола MAX"""
    body = msgpack.packb(payload, use_bin_type=True) if payload is not None else b""
    packed_len = len(body) & 0x00FFFFFF
    return FRAME_HEADER.pack(PROTOCOL_VERSION, cmd, seq, opcode, packed_len) + body


def unpack_frame(raw) -> Optional[Dict]:
    """Разбирает бинарный кадр протокола MAX"""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) < FRAME_HEADER.size:
        return None

    _, cmd, seq, opcode, packed_len = FRAME_HEADER.unpack_from(raw, 0)
    flags = (packed_len >> 24) & 0xFF
    payload_len = packed_len & 0x00FFFFFF
    total_len = FRAME_HEADER.size + payload_len
    if len(raw) < total_len:
        return None

    body = raw[FRAME_HEADER.size:total_len]
    if body and flags:
        if flags == 0xFF:
            if zstandard is None:
                raise ValueError("Для распаковки ответа нужен пакет zstandard")
            body = zstandard.ZstdDecompressor().decompressobj().decompress(body)
        elif flags > 0x7F:
            raise ValueError(f"Неизвестный тип сжатия: {flags}")
        else:
            body = lz4_decompress(body)

    payload = {}
    if body:
        payload = _normalize(msgpack.unpackb(body, raw=False, strict_map_key=False, ext_hook=_msgpack_ext_hook))

    return {
        "cmd": cmd,
        "seq": seq,
        "opcode": opcode,
        "payload": payload if isinstance(payload, dict) else {},
    }


def build_web_user_agent() -> Dict[str, Any]:
    """Формирует user-agent веб-клиента MAX"""
    locale, timezone = random.choice(LOCALE_TIMEZONES)
    return {
        "deviceType": "WEB",
        "locale": locale,
        "deviceLocale": locale,
        "osVersion": "Linux",
        "deviceName": "Chrome",
        "headerUserAgent": DEFAULT_USER_AGENT,
        "appVersion": WEB_APP_VERSION,
        "screen": WEB_SCREEN,
        "timezone": timezone,
    }


class Message(dict):
    """Объект входящего сообщения MAX с доступом как через атрибуты, так и через ключи словаря"""

    def __init__(self, raw_payload: Dict[str, Any], client: Optional["MaxClient"] = None):
        super().__init__(raw_payload or {})
        self._raw = raw_payload or {}
        self._client = client

        msg = self._raw.get("message")
        if not isinstance(msg, dict):
            msg = self._raw

        self.id = msg.get("id")
        self.chat_id = self._raw.get("chatId") or msg.get("chatId") or msg.get("chat_id")
        self.sender = msg.get("sender") or msg.get("author") or self._raw.get("sender")
        self.text = msg.get("text") or ""
        self.time = msg.get("time") or 0
        self.type = msg.get("type") or ""
        self.attaches = msg.get("attaches") or []
        self.elements = msg.get("elements") or []

    @property
    def chatId(self) -> Optional[int]:
        return self.chat_id

    async def answer(self, text: str) -> Optional[Dict]:
        """Отвечает в тот же чат"""
        if self._client and self.chat_id:
            return await self._client.send_message(self.chat_id, text)
        return None

    async def reply(self, text: str) -> Optional[Dict]:
        """Отвечает с цитированием текущего сообщения"""
        if self._client and self.chat_id and self.id:
            return await self._client.send_message(self.chat_id, text, reply_to=self.id)
        return None


class MaxClient:
    def __init__(
        self,
        token: Optional[str] = None,
        phone: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
        session_file: str = SESSION_FILE,
        session_name: Optional[str] = None,
        work_dir: str = ".",
    ):
        self.logger = logger or logging.getLogger("MaxClient")
        self.work_dir = work_dir
        if session_name:
            self.session_file = os.path.join(work_dir, session_name)
        else:
            self.session_file = session_file

        self.phone = phone

        session = self._load_session()
        self.token = token or session.get("token")
        self.device_id = session.get("deviceId") or str(uuid.uuid4())
        self.sync = session.get("sync") or {}

        self.user_agent = build_web_user_agent()
        for key in ("locale", "deviceLocale", "timezone"):
            saved = (session.get("userAgent") or {}).get(key)
            if saved:
                self.user_agent[key] = saved

        self.ws = None
        self.seq = -1
        self.pending_responses: Dict[int, asyncio.Future] = {}
        self.message_handlers: List[Callable] = []
        self.is_connected = False
        self._recv_task = None
        self._ping_task = None

        # Локальный кеш контактов и чатов для быстрого определения авторов
        self.users_cache: Dict[int, Dict[str, Any]] = {}
        self.chats_cache: Dict[int, Dict[str, Any]] = {}

    def _load_session(self) -> Dict:
        """Читает сохранённые токен, device_id и метки синхронизации из JSON или SQLite"""
        if self.session_file.endswith(".db"):
            return self._load_session_db()
        return self._load_session_json()

    def _load_session_json(self) -> Dict:
        try:
            with open(self.session_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _load_session_db(self) -> Dict:
        try:
            if not os.path.exists(self.session_file):
                return {}
            con = sqlite3.connect(self.session_file)
            con.row_factory = sqlite3.Row
            cursor = con.cursor()

            # 1. Проверяем таблицу sessions (формат pymax)
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions';")
            if cursor.fetchone():
                cursor.execute("SELECT * FROM sessions LIMIT 1;")
                row = cursor.fetchone()
                if row:
                    data = dict(row)
                    con.close()
                    return {
                        "token": data.get("token"),
                        "deviceId": data.get("device_id"),
                        "phone": data.get("phone"),
                        "sync": {
                            "chatsSync": data.get("chats_sync", -1),
                            "contactsSync": data.get("contacts_sync", -1),
                            "draftsSync": data.get("drafts_sync", -1),
                            "presenceSync": data.get("presence_sync", -1),
                            "configHash": data.get("config_hash", DEFAULT_CONFIG_HASH),
                        },
                    }

            # 2. Проверяем таблицу auth (старый формат mirea .db)
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='auth';")
            if cursor.fetchone():
                cursor.execute("SELECT * FROM auth LIMIT 1;")
                row = cursor.fetchone()
                if row:
                    data = dict(row)
                    con.close()
                    return {
                        "token": data.get("token"),
                        "deviceId": data.get("device_id"),
                    }

            con.close()
            return {}
        except Exception as e:
            self.logger.warning(f"Ошибка загрузки сессии из SQLite {self.session_file}: {e}")
            return {}

    def _save_session(self):
        """Сохраняет сессию в JSON или SQLite"""
        if self.session_file.endswith(".db"):
            self._save_session_db()
        else:
            self._save_session_json()

    def _save_session_json(self):
        try:
            folder = os.path.dirname(os.path.abspath(self.session_file))
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(self.session_file, "w", encoding="utf-8") as f:
                json.dump({
                    "deviceId": self.device_id,
                    "token": self.token,
                    "phone": self.phone,
                    "sync": self.sync,
                    "userAgent": self.user_agent,
                }, f, ensure_ascii=False, indent=2)
        except OSError as e:
            self.logger.warning(f"Не удалось сохранить сессию JSON: {e}")

    def _save_session_db(self):
        try:
            folder = os.path.dirname(os.path.abspath(self.session_file))
            if folder:
                os.makedirs(folder, exist_ok=True)
            con = sqlite3.connect(self.session_file)
            cursor = con.cursor()

            # Обновляем таблицу auth для совместимости
            cursor.execute("CREATE TABLE IF NOT EXISTS auth (token VARCHAR, device_id CHAR(32));")
            cursor.execute("DELETE FROM auth;")
            cursor.execute("INSERT INTO auth (token, device_id) VALUES (?, ?);", (self.token, self.device_id))

            # Обновляем таблицу sessions
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT NOT NULL PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    phone TEXT NOT NULL DEFAULT '',
                    mt_instance_id TEXT NOT NULL DEFAULT '',
                    chats_sync INTEGER NOT NULL DEFAULT -1,
                    contacts_sync INTEGER NOT NULL DEFAULT -1,
                    drafts_sync INTEGER NOT NULL DEFAULT -1,
                    presence_sync INTEGER NOT NULL DEFAULT -1,
                    config_hash TEXT NOT NULL DEFAULT ''
                );
            """)
            cursor.execute("""
                INSERT OR REPLACE INTO sessions (
                    token, device_id, phone, mt_instance_id,
                    chats_sync, contacts_sync, drafts_sync, presence_sync, config_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                self.token,
                self.device_id,
                self.phone or "",
                str(uuid.uuid4()),
                self.sync.get("chatsSync", -1),
                self.sync.get("contactsSync", -1),
                self.sync.get("draftsSync", -1),
                self.sync.get("presenceSync", -1),
                self.sync.get("configHash", DEFAULT_CONFIG_HASH),
            ))
            con.commit()
            con.close()
        except Exception as e:
            self.logger.warning(f"Не удалось сохранить сессию SQLite: {e}")

    async def connect(self):
        """Подключается к WebSocket, выполняет рукопожатие и вход"""
        if self.is_connected:
            return

        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass

        ssl_context = ssl.create_default_context()
        try:
            self.ws = await websockets.connect(
                WEBSOCKET_URI,
                ssl=ssl_context,
                origin=WEBSOCKET_ORIGIN,
                user_agent_header=DEFAULT_USER_AGENT,
                max_size=MAX_FRAME_SIZE,
            )
            self.is_connected = True
            self.seq = -1
            self._reset_pending()

            self._recv_task = asyncio.create_task(self._recv_loop())

            await self._handshake()
            if not self.token and self.phone:
                await self._login_flow()
            if not self.token:
                raise ValueError("Отсутствует токен или номер телефона")

            try:
                await self._login()
            except MaxApiError as e:
                if e.error not in INVALID_TOKEN_ERRORS or not self.phone:
                    raise
                self.logger.warning("Токен недействителен, требуется повторная авторизация")
                self.token, self.sync = None, {}
                self._save_session()
                await self._login_flow()
                await self._login()

            self.logger.info("Вход выполнен успешно")

            if not self._ping_task or self._ping_task.done():
                self._ping_task = asyncio.create_task(self._ping_loop())
        except Exception as e:
            self.is_connected = False
            self.logger.error(f"Ошибка подключения к MAX: {e}")
            raise

    async def start(self):
        """Запускает клиента и ожидает завершения работы (long-running)"""
        await self.connect()
        while self.is_connected or (self._recv_task and not self._recv_task.done()):
            await asyncio.sleep(1)

    async def close(self):
        """Останавливает клиента и закрывает соединения"""
        self.is_connected = False
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        self._reset_pending()

    async def _handshake(self):
        """Инициализирует сессию (SESSION_INIT)"""
        await self._send_and_wait(Opcode.SESSION_INIT, {
            "userAgent": self.user_agent,
            "deviceId": self.device_id,
        })

    async def _login(self):
        """Выполняет вход по токену и сохраняет метки синхронизации"""
        payload = await self._send_and_wait(Opcode.LOGIN, {
            "token": self.token,
            "chatsCount": 40,
            "interactive": True,
            "chatsSync": self.sync.get("chatsSync", -1),
            "contactsSync": self.sync.get("contactsSync", -1),
            "presenceSync": self.sync.get("presenceSync", -1),
            "draftsSync": self.sync.get("draftsSync", -1),
        })

        new_token = payload.get("token")
        if new_token:
            self.token = new_token

        self._update_sync(payload)

        # Кешируем полученные контакты и чаты
        for c in payload.get("contacts") or []:
            if isinstance(c, dict):
                self._cache_user(c)
        for ch in payload.get("chats") or []:
            if isinstance(ch, dict):
                self._cache_chat(ch)

        flags = payload.get("login2Flags") or {}
        if any(flags.get(k) for k in ("configEnabled", "contactEnabled", "profileEnabled")):
            await self._login2(flags)

        self._save_session()

    async def _login2(self, flags: Dict):
        """Догружает профиль и контакты (LOGIN2)"""
        payload = await self._send_and_wait(Opcode.LOGIN2, {
            "needProfile": bool(flags.get("profileEnabled")),
            "contactsSync": self.sync.get("contactsSync", -1) if flags.get("contactEnabled") else -1,
            "configHash": self.sync.get("configHash", DEFAULT_CONFIG_HASH),
        })
        self._update_sync(payload)

        for c in payload.get("contactInfos") or payload.get("contacts") or []:
            if isinstance(c, dict):
                self._cache_user(c)

    def _update_sync(self, payload: Dict):
        """Обновляет метки синхронизации из ответа сервера"""
        sync_time = payload.get("time")
        if sync_time is not None:
            for key in ("chatsSync", "contactsSync", "draftsSync", "presenceSync"):
                self.sync[key] = sync_time

        config_hash = (payload.get("config") or {}).get("hash")
        if config_hash is not None:
            self.sync["configHash"] = config_hash

    async def _login_flow(self):
        """Вход по номеру телефона через консоль"""
        temp_token = await self.request_code(self.phone)
        code = await self._ask(f"Введите код для {self.phone}: ")
        payload = await self.send_code(code, temp_token)

        token = self._extract_login_token(payload)
        if not token and payload.get("passwordChallenge"):
            token = await self._password_flow(payload["passwordChallenge"])

        if not token:
            raise Exception("Не удалось получить токен")

        self.token = token
        self._save_session()

    async def _password_flow(self, challenge: Dict) -> Optional[str]:
        """Проверка пароля двухфакторной аутентификации"""
        hint = challenge.get("hint")
        prompt = f"Введите пароль двухфакторной аутентификации{f' (подсказка: {hint})' if hint else ''}: "
        password = await self._ask(prompt)
        payload = await self._send_and_wait(Opcode.AUTH_CHECK_PASSWORD, {
            "trackId": challenge.get("trackId"),
            "password": password,
        })
        return self._extract_login_token(payload)

    @staticmethod
    async def _ask(prompt: str) -> str:
        print(prompt, end="", flush=True)
        return (await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)).strip()

    @staticmethod
    def _extract_login_token(payload: Dict) -> Optional[str]:
        return ((payload.get("tokenAttrs") or {}).get("LOGIN") or {}).get("token")

    @staticmethod
    def _format_phone(phone: str) -> str:
        digits = "".join(c for c in phone if c.isdigit())
        return f"+{digits}" if digits else phone

    async def request_code(self, phone: str) -> str:
        payload = await self._send_and_wait(Opcode.AUTH_REQUEST, {
            "phone": self._format_phone(phone),
            "type": "START_AUTH",
        })
        return payload.get("token")

    async def send_code(self, code: str, token: str) -> Dict:
        return await self._send_and_wait(Opcode.AUTH, {
            "token": token,
            "verifyCode": code,
            "authTokenType": "CHECK_CODE",
        })

    def _cache_user(self, user: Dict[str, Any]):
        uid = user.get("id")
        if uid is not None:
            self.users_cache[uid] = user

    def _cache_chat(self, chat: Dict[str, Any]):
        cid = chat.get("id")
        if cid is not None:
            self.chats_cache[cid] = chat

    async def get_users(self, user_ids: List[int]) -> List[Dict[str, Any]]:
        """Запрашивает информацию о пользователях через Opcode.CONTACT_INFO (32)"""
        if not user_ids:
            return []
        try:
            payload = await self._send_and_wait(Opcode.CONTACT_INFO, {
                "contactIds": user_ids
            })
            contacts = payload.get("contacts") or []
            for c in contacts:
                if isinstance(c, dict):
                    self._cache_user(c)
            return contacts
        except Exception as e:
            self.logger.warning(f"Ошибка получения пользователей {user_ids}: {e}")
            return []

    async def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Возвращает информацию о пользователе из кеша или загружает с сервера"""
        if not user_id:
            return None
        if user_id in self.users_cache:
            return self.users_cache[user_id]
        users = await self.get_users([user_id])
        return users[0] if users else None

    def format_user_name(self, user: Optional[Dict[str, Any]]) -> str:
        """Форматирует имя и фамилию (если есть) или название бота"""
        if not isinstance(user, dict):
            return ""

        names = user.get("names") or []
        if isinstance(names, list):
            for n in names:
                if not isinstance(n, dict):
                    continue
                first = (n.get("firstName") or n.get("first_name") or "").strip()
                last = (n.get("lastName") or n.get("last_name") or "").strip()
                full = (n.get("name") or "").strip()
                if first and last:
                    return f"{first} {last}"
                if first:
                    return first
                if last:
                    return last
                if full:
                    return full

        first = (user.get("firstName") or user.get("first_name") or "").strip()
        last = (user.get("lastName") or user.get("last_name") or "").strip()
        if first and last:
            return f"{first} {last}"
        if first:
            return first
        if last:
            return last

        full = (
            user.get("name")
            or user.get("title")
            or user.get("nick")
            or user.get("nickname")
            or ""
        ).strip()
        if full:
            return full

        return ""

    async def get_user_name(self, user_id: int) -> str:
        """Возвращает имя пользователя или название бота"""
        if not user_id:
            return ""
        if user_id in self.users_cache:
            name = self.format_user_name(self.users_cache[user_id])
            if name:
                return name
        user = await self.get_user(user_id)
        if user:
            return self.format_user_name(user)
        return ""

    async def get_chat_info(self, chat_id: int) -> Optional[Dict[str, Any]]:
        """Запрашивает информацию о чате через Opcode.CHAT_INFO (48)"""
        if not chat_id:
            return None
        if chat_id in self.chats_cache:
            return self.chats_cache[chat_id]
        try:
            payload = await self._send_and_wait(Opcode.CHAT_INFO, {
                "chatIds": [chat_id]
            })
            chats = payload.get("chats") or []
            for ch in chats:
                if isinstance(ch, dict):
                    self._cache_chat(ch)
            return chats[0] if chats else None
        except Exception as e:
            self.logger.warning(f"Ошибка получения чата {chat_id}: {e}")
            return None

    async def get_chat_title(self, chat_id: int) -> str:
        """Возвращает название чата"""
        if not chat_id:
            return ""
        if chat_id in self.chats_cache:
            return self.chats_cache[chat_id].get("title", "")
        chat = await self.get_chat_info(chat_id)
        return chat.get("title", "") if chat else ""

    async def send_message(self, chat_id: int, text: str, reply_to: Optional[int] = None) -> Dict:
        """Отправляет текстовое сообщение в чат MAX"""
        cid = random.randint(100000, 999999)
        msg_obj: Dict[str, Any] = {
            "cid": cid,
            "text": text,
            "type": "USER",
        }
        if reply_to:
            msg_obj["link"] = {"messageId": str(reply_to), "type": "REPLY"}
        payload = {
            "chatId": chat_id,
            "message": msg_obj,
            "notify": True,
        }
        return await self._send_and_wait(Opcode.MSG_SEND, payload)

    async def download_file(self, url: str) -> bytes:
        """Скачивает файл/изображение/видео по URL"""
        if url.startswith("//"):
            url = f"https:{url}"
        headers = {"User-Agent": DEFAULT_USER_AGENT}
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers) as r:
                r.raise_for_status()
                return await r.read()

    async def get_video_url(self, chat_id: int, message_id: Union[int, str], video_id: int) -> str:
        """Возвращает ссылку на видео в максимальном доступном качестве"""
        payload = await self._send_and_wait(Opcode.VIDEO_PLAY, {
            "chatId": chat_id, "messageId": message_id, "videoId": video_id
        })

        mp4_urls = []
        for key, url in payload.items():
            if not isinstance(key, str) or not isinstance(url, str):
                continue
            normalized = key.upper()
            if not normalized.startswith("MP4_"):
                continue
            try:
                quality = int(normalized.removeprefix("MP4_"))
            except ValueError:
                continue
            if quality > 0 and url.strip():
                mp4_urls.append((quality, url.strip()))

        if mp4_urls:
            selected_url = max(mp4_urls, key=lambda item: item[0])[1]
            if selected_url.startswith("//"):
                selected_url = f"https:{selected_url}"
            return selected_url

        url = (
            payload.get("url")
            or payload.get("dynamicUrl")
            or payload.get("dynamic_url")
            or payload.get("videoUrl")
            or payload.get("video_url")
            or payload.get("downloadUrl")
        )
        if not url:
            raise Exception("URL видео не найден")
        if url.startswith("//"):
            url = f"https:{url}"
        return url

    async def get_file_url(self, chat_id: int, message_id: Union[int, str], file_id: int) -> str:
        """Возвращает прямую ссылку для скачивания файла"""
        payload = await self._send_and_wait(Opcode.FILE_DOWNLOAD, {
            "chatId": chat_id, "messageId": message_id, "fileId": file_id
        })
        url = payload.get("url") or payload.get("downloadUrl") or payload.get("fileUrl")
        if not url:
            raise Exception("URL файла не найден")
        if url.startswith("//"):
            url = f"https:{url}"
        return url

    async def join_chat(self, link: str) -> Dict:
        idx = link.find("join/")
        if idx == -1:
            raise ValueError("Неверная ссылка")
        payload = await self._send_and_wait(Opcode.CHAT_JOIN, {"link": link[idx:]})
        return payload.get("chat")

    def on_message(self, func: Optional[Callable] = None):
        """Декоратор для обработки входящих сообщений"""
        def decorator(handler: Callable):
            self.add_message_handler(handler)
            return handler

        if func is not None:
            return decorator(func)
        return decorator

    def add_message_handler(self, handler: Callable):
        if handler not in self.message_handlers:
            self.message_handlers.append(handler)

    async def _safe_call_handler(self, handler: Callable, msg: Message):
        try:
            res = handler(msg)
            if asyncio.iscoroutine(res):
                await res
        except Exception as e:
            self.logger.error(f"Исключение в обработчике сообщений: {e}", exc_info=True)

    def _next_seq(self) -> int:
        self.seq = (self.seq + 1) % 0x10000
        return self.seq

    def _reset_pending(self):
        for f in list(self.pending_responses.values()):
            if not f.done():
                f.set_exception(ConnectionError("Соединение сброшено"))
        self.pending_responses.clear()

    async def _send_and_wait(self, opcode: Opcode, payload: Dict, timeout: float = REQUEST_TIMEOUT) -> Dict:
        if not self.is_connected and opcode not in (
            Opcode.SESSION_INIT, Opcode.LOGIN, Opcode.LOGIN2,
            Opcode.AUTH, Opcode.AUTH_REQUEST, Opcode.AUTH_CHECK_PASSWORD
        ):
            await self.connect()

        seq = self._next_seq()
        f = asyncio.get_event_loop().create_future()
        self.pending_responses[seq] = f
        try:
            await self.ws.send(pack_frame(Command.REQUEST, seq, int(opcode), payload))
            frame = await asyncio.wait_for(f, timeout=timeout)
        finally:
            self.pending_responses.pop(seq, None)

        response = frame.get("payload") or {}
        if frame.get("cmd") == Command.ERROR or response.get("error"):
            raise MaxApiError(int(opcode), response)
        return response

    async def _recv_loop(self):
        try:
            async for raw in self.ws:
                try:
                    frame = unpack_frame(raw)
                except Exception as e:
                    self.logger.error(f"Ошибка разбора кадра: {e}")
                    continue
                if not frame:
                    continue

                seq, cmd, opcode = frame.get("seq"), frame.get("cmd"), frame.get("opcode")
                if cmd in (Command.RESPONSE, Command.ERROR) and seq in self.pending_responses:
                    f = self.pending_responses.get(seq)
                    if f and not f.done():
                        f.set_result(frame)
                elif cmd == Command.REQUEST and opcode == Opcode.NOTIF_MESSAGE:
                    payload = frame.get("payload", {})
                    msg_obj = Message(payload, client=self)
                    for h in list(self.message_handlers):
                        asyncio.create_task(self._safe_call_handler(h, msg_obj))
        except Exception as e:
            self.logger.warning(f"Соединение с MAX потеряно: {e}")
        finally:
            self.is_connected = False
            self._reset_pending()

    async def _ping_loop(self):
        while True:
            if self.is_connected:
                try:
                    await self._send_and_wait(Opcode.PING, {"interactive": True}, timeout=10.0)
                except Exception:
                    self.is_connected = False
            else:
                try:
                    await self.connect()
                except Exception:
                    await asyncio.sleep(5)
            await asyncio.sleep(30)
