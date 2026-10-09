import html
import json
from typing import Any, Dict, List, Optional


def utf16_offset_to_char_index(s: str, utf16_offset: int) -> int:
    """Преобразует смещение в UTF-16 code units (как в MAX API) в индекс символа Python"""
    if utf16_offset <= 0:
        return 0
    encoded = s.encode("utf-16-le")
    byte_offset = utf16_offset * 2
    if byte_offset >= len(encoded):
        return len(s)
    return len(encoded[:byte_offset].decode("utf-16-le", errors="ignore"))


def max_elements_to_html(text: str, elements: List[Dict]) -> str:
    """Конвертирует элементы форматирования MAX в HTML Telegram"""
    if not text:
        return ""
    if not elements:
        return html.escape(text)

    tag_map = {
        "STRONG": ("<b>", "</b>"),
        "HEADING": ("<b>", "</b>"),
        "EMPHASIZED": ("<i>", "</i>"),
        "UNDERLINE": ("<u>", "</u>"),
        "STRIKETHROUGH": ("<s>", "</s>"),
        "MONOSPACED": ("<code>", "</code>"),
        "CODE": ("<pre><code>", "</code></pre>"),
        "QUOTE": ("<blockquote>", "</blockquote>"),
    }

    events = []
    for i, e in enumerate(elements):
        start_u16 = e.get("from", 0)
        len_u16 = e.get("length", 0)

        # Конвертируем UTF-16 смещения в символьные индексы строки Python
        start = utf16_offset_to_char_index(text, start_u16)
        end = utf16_offset_to_char_index(text, start_u16 + len_u16)

        events.append((start, i, False, e))
        events.append((end, -i, True, e))

    events.sort(key=lambda x: (x[0], not x[2]))

    result, last_pos, active_tags = [], 0, []

    for pos, priority, is_closing, e in events:
        result.append(html.escape(text[last_pos:pos]))

        etype = e.get("type")
        if etype == "LINK":
            url = html.escape(e.get("attributes", {}).get("url", ""))
            tags = (f'<a href="{url}">', "</a>")
        else:
            tags = tag_map.get(etype, ("", ""))

        if is_closing:
            temp_stack = []
            while active_tags:
                curr_tags, curr_e = active_tags.pop()
                result.append(curr_tags[1])
                if curr_e == e:
                    break
                temp_stack.append((curr_tags, curr_e))
            while temp_stack:
                reopen_tags, reopen_e = temp_stack.pop()
                result.append(reopen_tags[0])
                active_tags.append((reopen_tags, reopen_e))
        else:
            result.append(tags[0])
            active_tags.append((tags, e))
        last_pos = pos

    result.append(html.escape(text[last_pos:]))
    return "".join(result)


def max_elements_to_vk_format(elements: List[Dict], offset_shift: int = 0, author_len: int = 0) -> str:
    """Конвертирует элементы форматирования MAX в JSON format_data для API ВКонтакте"""
    vk_items = []

    if author_len > 0:
        vk_items.append({"offset": 0, "length": author_len, "type": "bold"})

    if elements:
        tag_map = {
            "STRONG": "bold",
            "HEADING": "bold",
            "EMPHASIZED": "italic",
            "UNDERLINE": "underline",
            "LINK": "url",
            "MONOSPACED": "mono",
            "CODE": "mono",
        }
        for e in elements:
            etype = e.get("type")
            if etype not in tag_map:
                continue
            item = {
                "offset": e.get("from", 0) + offset_shift,
                "length": e.get("length", 0),
                "type": tag_map[etype],
            }
            if etype == "LINK":
                url = e.get("attributes", {}).get("url", "")
                if url:
                    item["url"] = url
            vk_items.append(item)

    return json.dumps({"version": "1", "items": vk_items}, ensure_ascii=False) if vk_items else ""


def extract_max_buttons(msg: dict) -> List[List[Dict[str, Any]]]:
    """Извлекает и нормализует инлайн-кнопки из сообщения MAX.
    Возвращает список рядов кнопок: [[{"text": str, "url": Optional[str], "data": Optional[str]}]]
    """
    if not isinstance(msg, dict):
        return []

    raw_candidates = []

    # 1. Поиск во вложениях attaches
    for a in msg.get("attaches", []):
        if not isinstance(a, dict):
            continue
        atype = a.get("_type") or a.get("type")
        if atype in ("INLINE_KEYBOARD", "KEYBOARD"):
            raw_candidates.append(a.get("keyboard") or a.get("buttons") or a.get("rows") or a)

    # 2. Поиск в полях самого сообщения
    for key in ("keyboard", "inline_keyboard", "reply_markup"):
        val = msg.get(key)
        if val:
            raw_candidates.append(val)

    if not raw_candidates:
        return []

    result_rows: List[List[Dict[str, Any]]] = []

    for raw in raw_candidates:
        rows_data = []
        if isinstance(raw, dict):
            btn_container = (
                raw.get("buttons")
                or raw.get("rows")
                or raw.get("inline_keyboard")
                or raw.get("keyboard")
            )
            if btn_container is not None:
                rows_data = btn_container
            elif "text" in raw or "title" in raw or "label" in raw:
                rows_data = [[raw]]
        elif isinstance(raw, list):
            rows_data = raw

        if not isinstance(rows_data, list):
            continue

        for item in rows_data:
            current_row = []
            if isinstance(item, list):
                btn_list = item
            elif isinstance(item, dict):
                btn_list = [item]
            else:
                continue

            for btn in btn_list:
                if not isinstance(btn, dict):
                    continue

                action = btn.get("action") if isinstance(btn.get("action"), dict) else {}

                text = (
                    btn.get("text")
                    or btn.get("title")
                    or btn.get("label")
                    or btn.get("caption")
                    or btn.get("name")
                    or action.get("label")
                    or action.get("text")
                    or ""
                )
                text = str(text).strip()
                if not text:
                    continue

                url = (
                    btn.get("url")
                    or btn.get("link")
                    or btn.get("open_url")
                    or action.get("link")
                    or action.get("url")
                )
                if not url and isinstance(btn.get("web_app"), dict):
                    url = btn.get("web_app", {}).get("url")

                url_str = str(url).strip() if url else None
                if url_str and not (
                    url_str.startswith("http://")
                    or url_str.startswith("https://")
                    or url_str.startswith("tg://")
                ):
                    url_str = "https://" + url_str

                data = (
                    btn.get("payload")
                    or btn.get("data")
                    or btn.get("callback_data")
                    or action.get("payload")
                    or action.get("data")
                )
                data_str = str(data).strip() if data is not None else None

                current_row.append({"text": text, "url": url_str, "data": data_str})

            if current_row:
                result_rows.append(current_row)

        if result_rows:
            break

    return result_rows


def build_tg_keyboard(button_rows: List[List[Dict[str, Any]]]):
    """Строит InlineKeyboardMarkup для aiogram из списка рядов кнопок MAX"""
    if not button_rows:
        return None
    try:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    except ImportError:
        return None

    tg_rows = []
    for r_idx, row in enumerate(button_rows):
        tg_row = []
        for c_idx, btn in enumerate(row):
            text = btn.get("text", "")
            if not text:
                continue
            url = btn.get("url")
            data = btn.get("data")
            if url:
                if not (url.startswith("http://") or url.startswith("https://") or url.startswith("tg://")):
                    url = "https://" + url
                tg_row.append(InlineKeyboardButton(text=text, url=url))
            else:
                cb = f"m_{data}" if data else f"m_cb_{r_idx}_{c_idx}"
                cb = cb.encode("utf-8")[:64].decode("utf-8", errors="ignore")
                tg_row.append(InlineKeyboardButton(text=text, callback_data=cb))
        if tg_row:
            # Telegram разрешает до 8 кнопок в ряду
            for i in range(0, len(tg_row), 8):
                tg_rows.append(tg_row[i : i + 8])

    return InlineKeyboardMarkup(inline_keyboard=tg_rows) if tg_rows else None


def build_vk_keyboard(button_rows: List[List[Dict[str, Any]]]) -> Optional[str]:
    """Строит JSON-строку инлайн-клавиатуры для API ВКонтакте из списка рядов кнопок MAX"""
    if not button_rows:
        return None

    vk_rows = []
    for r_idx, row in enumerate(button_rows):
        current_row = []
        for c_idx, btn in enumerate(row):
            # ВКонтакте ограничивает длину текста кнопки 40 символами
            text = (btn.get("text") or "")[:40]
            if not text:
                continue
            url = btn.get("url")
            data = btn.get("data")
            if url:
                if not (url.startswith("http://") or url.startswith("https://")):
                    url = "https://" + url
                current_row.append({
                    "action": {
                        "type": "open_link",
                        "link": url,
                        "label": text,
                    }
                })
            else:
                payload = json.dumps(
                    {"cmd": "max_cb", "data": (data or f"{r_idx}_{c_idx}")[:100]},
                    ensure_ascii=False,
                )
                current_row.append({
                    "action": {
                        "type": "text",
                        "label": text,
                        "payload": payload,
                    },
                    "color": "secondary",
                })
        if current_row:
            # ВКонтакте разрешает до 5 кнопок в одном ряду инлайн-клавиатуры
            for i in range(0, len(current_row), 5):
                vk_rows.append(current_row[i : i + 5])

    # ВКонтакте разрешает максимум 6 рядов для инлайн-клавиатуры
    vk_rows = vk_rows[:6]
    if not vk_rows:
        return None

    return json.dumps({"inline": True, "buttons": vk_rows}, ensure_ascii=False)


def extract_max_polls(msg: dict) -> List[Dict[str, Any]]:
    """Извлекает и нормализует опросы из сообщения MAX.
    Возвращает список словарей со свойствами опроса:
    [{
        "question": str,
        "options": List[str],
        "is_anonymous": bool,
        "allows_multiple_answers": bool,
        "is_closed": bool,
        "is_quiz": bool,
    }]
    """
    if not isinstance(msg, dict):
        return []

    polls = []
    attaches = msg.get("attaches", [])
    if not isinstance(attaches, list):
        return []

    for a in attaches:
        if not isinstance(a, dict):
            continue
        atype = str(a.get("_type") or a.get("type") or "").upper()
        if atype != "POLL":
            continue

        question = a.get("title") or a.get("question") or a.get("text") or ""
        question = str(question).strip()

        raw_answers = a.get("answers") or a.get("options") or []
        options = []
        if isinstance(raw_answers, list):
            for ans in raw_answers:
                if isinstance(ans, str):
                    text = ans.strip()
                elif isinstance(ans, dict):
                    text = str(
                        ans.get("text")
                        or ans.get("title")
                        or ans.get("value")
                        or ans.get("answer")
                        or ""
                    ).strip()
                else:
                    text = str(ans).strip()
                if text:
                    options.append(text)

        if not question and not options:
            continue
        if not question:
            question = "Опрос"

        # Разбор настроек PollFlags:
        # FLAG_SETTINGS_ANONYMOUS = 1
        # FLAG_SETTINGS_MULTISELECT = 2
        # FLAG_SETTINGS_REVOTE = 4
        # FLAG_SETTINGS_CLOSED = 8
        # FLAG_SETTINGS_QUIZ = 16
        settings = a.get("settings", 0)
        is_anonymous = True
        allows_multiple_answers = False
        is_closed = False
        is_quiz = False

        if isinstance(settings, int):
            is_anonymous = bool(settings & 1)
            allows_multiple_answers = bool(settings & 2)
            is_closed = bool(settings & 8)
            is_quiz = bool(settings & 16)
        elif isinstance(settings, dict):
            is_anonymous = bool(settings.get("anonymous", settings.get("is_anonymous", True)))
            allows_multiple_answers = bool(settings.get("multiselect", settings.get("is_multiple", False)))
            is_closed = bool(settings.get("closed", settings.get("is_closed", False)))
            is_quiz = bool(settings.get("quiz", settings.get("is_quiz", False)))
        else:
            if "is_anonymous" in a or "anonymous" in a:
                is_anonymous = bool(a.get("is_anonymous", a.get("anonymous")))
            if "is_multiple" in a or "multiple" in a or "multiselect" in a:
                allows_multiple_answers = bool(a.get("is_multiple", a.get("multiple", a.get("multiselect"))))
            if "is_closed" in a or "closed" in a:
                is_closed = bool(a.get("is_closed", a.get("closed")))
            if "is_quiz" in a or "quiz" in a:
                is_quiz = bool(a.get("is_quiz", a.get("quiz")))

        polls.append({
            "question": question,
            "options": options,
            "is_anonymous": is_anonymous,
            "allows_multiple_answers": allows_multiple_answers,
            "is_closed": is_closed,
            "is_quiz": is_quiz,
        })

    return polls


