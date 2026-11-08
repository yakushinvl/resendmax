import time
from typing import List, Optional, Union
from sqlmodel import Session, create_engine, func, select
from models import ChatMapping, PendingConnection, ProcessedMessage, SQLModel, UserLimit

# Настройки БД
sqlite_url = "sqlite:///database.db"
engine = create_engine(sqlite_url)

# Быстрый кеш недавних обработанных сообщений в оперативной памяти (chat_id, str_msg_id) -> timestamp
_recent_processed_cache: dict[tuple[int, str], float] = {}
_MAX_CACHE_SIZE = 10000

def create_db_and_tables():
    """Создает все необходимые таблицы"""
    SQLModel.metadata.create_all(engine)

def is_message_processed(max_chat_id: int, message_id: Union[int, str]) -> bool:
    """Проверяет, было ли сообщение уже обработано (отправлено или удалено)"""
    str_msg_id = str(message_id)
    cache_key = (max_chat_id, str_msg_id)

    # 1. Проверка в быстром in-memory кеше
    if cache_key in _recent_processed_cache:
        return True

    # 2. Проверка в базе данных
    with Session(engine) as session:
        statement = select(ProcessedMessage).where(
            ProcessedMessage.max_chat_id == max_chat_id,
            ProcessedMessage.message_id == str_msg_id
        )
        found = session.exec(statement).first() is not None
        if found:
            _recent_processed_cache[cache_key] = time.time()
        return found

def mark_message_processed(max_chat_id: int, message_id: Union[int, str], status: str = "SENT"):
    """Записывает сообщение как обработанное в БД и обновляет in-memory кеш"""
    global _recent_processed_cache
    str_msg_id = str(message_id)
    cache_key = (max_chat_id, str_msg_id)
    now = int(time.time())

    # Обновляем in-memory кеш
    if len(_recent_processed_cache) > _MAX_CACHE_SIZE:
        # Прореживаем половину самых старых записей
        sorted_keys = sorted(_recent_processed_cache.keys(), key=lambda k: _recent_processed_cache[k])
        for k in sorted_keys[: len(sorted_keys) // 2]:
            _recent_processed_cache.pop(k, None)
    _recent_processed_cache[cache_key] = float(now)

    # Записываем в SQLite
    with Session(engine) as session:
        statement = select(ProcessedMessage).where(
            ProcessedMessage.max_chat_id == max_chat_id,
            ProcessedMessage.message_id == str_msg_id
        )
        existing = session.exec(statement).first()
        if existing:
            existing.status = status
            existing.processed_at = now
            session.add(existing)
        else:
            record = ProcessedMessage(
                max_chat_id=max_chat_id,
                message_id=str_msg_id,
                status=status,
                processed_at=now
            )
            session.add(record)
        session.commit()

def cleanup_old_processed_messages(days: int = 7):
    """Очищает старые записи дедупликации (старше days дней)"""
    cutoff = int(time.time()) - (days * 86400)
    with Session(engine) as session:
        statement = select(ProcessedMessage).where(ProcessedMessage.processed_at < cutoff)
        old_records = session.exec(statement).all()
        for r in old_records:
            session.delete(r)
        session.commit()

def get_stats():
    """Возвращает статистику по чатам и пользователям"""
    with Session(engine) as session:
        vk_chats = session.exec(select(func.count()).select_from(ChatMapping).where(ChatMapping.platform == "vk")).one()
        tg_chats = session.exec(select(func.count()).select_from(ChatMapping).where(ChatMapping.platform == "tg")).one()
        unique_max_chats = session.exec(select(func.count(func.distinct(ChatMapping.max_chat_id)))).one()
        vk_users = session.exec(select(func.count(func.distinct(ChatMapping.owner_id))).where(ChatMapping.owner_platform == "vk")).one()
        tg_users = session.exec(select(func.count(func.distinct(ChatMapping.owner_id))).where(ChatMapping.owner_platform == "tg")).one()
        return {
            "vk_chats": vk_chats, "tg_chats": tg_chats,
            "vk_users": vk_users, "tg_users": tg_users,
            "unique_max_chats": unique_max_chats
        }

def set_user_limit(user_id: int, platform: str, limit: int):
    """Обновляет или создает лимит для пользователя"""
    with Session(engine) as session:
        db_limit = session.exec(select(UserLimit).where(UserLimit.user_platform_id == user_id, UserLimit.platform == platform)).first()
        if db_limit:
            db_limit.max_limit = limit
        else:
            db_limit = UserLimit(user_platform_id=user_id, platform=platform, max_limit=limit)
        session.add(db_limit)
        session.commit()

def get_user_limit(user_id: int, platform: str) -> int:
    """Возвращает текущий лимит пользователя (дефолт 3)"""
    with Session(engine) as session:
        db_limit = session.exec(select(UserLimit).where(UserLimit.user_platform_id == user_id, UserLimit.platform == platform)).first()
        return db_limit.max_limit if db_limit else 3

def add_chat_mapping(max_chat_id: int, max_chat_title: str, platform: str, target_chat_id: int, owner_id: int, owner_platform: str, target_thread_id: Optional[int] = None) -> bool:
    """Добавляет новую привязку, если такой еще нет"""
    with Session(engine) as session:
        statement = select(ChatMapping).where(
            ChatMapping.max_chat_id == max_chat_id,
            ChatMapping.platform == platform,
            ChatMapping.target_chat_id == target_chat_id,
            ChatMapping.target_thread_id == target_thread_id
        )
        if session.exec(statement).first(): return False
        mapping = ChatMapping(
            max_chat_id=max_chat_id, max_chat_title=max_chat_title,
            platform=platform, target_chat_id=target_chat_id,
            target_thread_id=target_thread_id,
            owner_id=owner_id, owner_platform=owner_platform
        )
        session.add(mapping)
        session.commit()
        return True

def get_user_mappings(owner_id: int, owner_platform: str) -> List[ChatMapping]:
    """Возвращает все привязки пользователя"""
    with Session(engine) as session:
        return session.exec(select(ChatMapping).where(ChatMapping.owner_id == owner_id, ChatMapping.owner_platform == owner_platform)).all()

def delete_chat_mapping(mapping_id: int):
    """Удаляет привязку по ID"""
    with Session(engine) as session:
        mapping = session.get(ChatMapping, mapping_id)
        if mapping:
            session.delete(mapping)
            session.commit()

def get_mappings_for_max_chat(max_chat_id: int) -> List[ChatMapping]:
    """Возвращает список привязок для конкретного чата MAX"""
    with Session(engine) as session:
        return session.exec(select(ChatMapping).where(ChatMapping.max_chat_id == max_chat_id)).all()

def add_pending_connection(code: str, max_chat_id: int, max_chat_title: str, user_platform_id: int, platform: str):
    """Создает временную запись ожидания привязки для процесса соединения через код"""
    with Session(engine) as session:
        pending = PendingConnection(
            code=code, max_chat_id=max_chat_id, max_chat_title=max_chat_title,
            user_platform_id=user_platform_id, platform=platform
        )
        session.add(pending)
        session.commit()

def pop_pending_connection(code: str) -> Optional[PendingConnection]:
    """Извлекает и удаляет временную запись по коду"""
    with Session(engine) as session:
        pending = session.exec(select(PendingConnection).where(PendingConnection.code == code)).first()
        if pending:
            session.delete(pending)
            session.commit()
            return pending
        return None
