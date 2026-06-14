from typing import Dict, List
import json

def max_elements_to_html(text: str, elements: List[Dict]) -> str:
    """Конвертирует элементы форматирования MAX в HTML Telegram"""
    if not text:
        return ""
    if not elements:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    tag_map = {
        "STRONG": ("<b>", "</b>"), "HEADING": ("<b>", "</b>"),
        "EMPHASIZED": ("<i>", "</i>"), "UNDERLINE": ("<u>", "</u>"),
        "STRIKETHROUGH": ("<s>", "</s>"), "MONOSPACED": ("<code>", "</code>"),
        "QUOTE": ("<blockquote>", "</blockquote>")
    }

    events = []
    for i, e in enumerate(elements):
        start, length = e.get('from', 0), e.get('length', 0)
        events.append((start, i, False, e))
        events.append((start + length, -i, True, e))

    events.sort(key=lambda x: (x[0], not x[2]))

    result, last_pos, active_tags = [], 0, []

    for pos, priority, is_closing, e in events:
        result.append(text[last_pos:pos].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        
        etype = e.get('type')
        tags = (f'<a href="{e.get("attributes", {}).get("url", "")}">', "</a>") if etype == "LINK" else tag_map.get(etype, ("", ""))

        if is_closing:
            temp_stack = []
            while active_tags:
                curr_tags, curr_e = active_tags.pop()
                result.append(curr_tags[1])
                if curr_e == e: break
                temp_stack.append((curr_tags, curr_e))
            while temp_stack:
                reopen_tags, reopen_e = temp_stack.pop()
                result.append(reopen_tags[0])
                active_tags.append((reopen_tags, reopen_e))
        else:
            result.append(tags[0])
            active_tags.append((tags, e))
        last_pos = pos

    result.append(text[last_pos:].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return "".join(result)

def max_elements_to_vk_format(elements: List[Dict]) -> str:
    """Конвертирует элементы форматирования MAX в JSON format_data для API ВКонтакте"""
    if not elements: return ""
    
    vk_items = []
    tag_map = {"STRONG": "bold", "HEADING": "bold", "EMPHASIZED": "italic", "UNDERLINE": "underline", "LINK": "url"}

    for e in elements:
        etype = e.get('type')
        if etype not in tag_map: continue
        item = {"offset": e.get('from', 0), "length": e.get('length', 0), "type": tag_map[etype]}
        if etype == "LINK": item["url"] = e.get('attributes', {}).get('url', '')
        vk_items.append(item)
    
    return json.dumps({"version": "1", "items": vk_items}) if vk_items else ""
