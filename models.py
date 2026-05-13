from typing import Optional
from sqlmodel import Field, SQLModel

class ChatMapping(SQLModel, table=True):
    """Модель привязки чата MAX к целевой группе ВКонтакте/Telegram"""
    id: Optional[int] = Field(default=None, primary_key=True)
    max_chat_id: int = Field(index=True)
    max_chat_title: str = Field(default="Неизвестный чат")
    platform: str  # "vk" или "tg"
    target_chat_id: int
    target_thread_id: Optional[int] = None  # ID топика (для Telegram)
    owner_id: int  # ID пользователя-создателя
    owner_platform: str  # "vk" или "tg"


class PendingConnection(SQLModel, table=True):
    """Временная запись для процесса привязки чата через код"""
    code: str = Field(primary_key=True)
    max_chat_id: int
    max_chat_title: str = Field(default="Неизвестный чат")
    user_max_id: Optional[int] = None
    user_platform_id: int
    platform: str

class UserLimit(SQLModel, table=True):
    """Индивидуальные лимиты пользователей на количество чатов"""
    user_platform_id: int = Field(primary_key=True)
    platform: str = Field(primary_key=True)
    max_limit: int = Field(default=3)
