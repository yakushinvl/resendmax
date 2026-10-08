import html
import json
from typing import Dict, List, Optional


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
