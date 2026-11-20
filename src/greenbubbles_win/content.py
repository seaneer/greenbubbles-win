"""Decode WeChat 4.x message rows into human-readable text.

The packed ``local_type`` is ``(sub_type << 32) | msg_type``. Content is
usually UTF-8 text or XML; WCDB may store it zstd-compressed, which the
``WCDB_CT_message_content`` marker (4) or the zstd magic bytes reveal.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import zstandard

MSG_TYPE_TEXT = 1
MSG_TYPE_IMAGE = 3
MSG_TYPE_VOICE = 34
MSG_TYPE_VIDEO = 43
MSG_TYPE_EMOJI = 47
MSG_TYPE_LOCATION = 48
MSG_TYPE_APP = 49
MSG_TYPE_SYSTEM = 10000
MSG_TYPE_REVOKE = 10002

TYPE_LABELS = {
    MSG_TYPE_TEXT: "text",
    MSG_TYPE_IMAGE: "image",
    MSG_TYPE_VOICE: "voice",
    MSG_TYPE_VIDEO: "video",
    MSG_TYPE_EMOJI: "emoji",
    MSG_TYPE_LOCATION: "location",
    MSG_TYPE_APP: "app",
    MSG_TYPE_SYSTEM: "system",
    MSG_TYPE_REVOKE: "revoke",
}

SUBTYPE_LABELS = {
    1: "text_share",
    2: "image_share",
    3: "audio_share",
    4: "video_share",
    5: "link",
    6: "file",
    7: "webview",
    8: "gif",
    10: "location_sharing",
    13: "brand",
    14: "chat_log_backup",
    15: "chat_log_migrate",
    16: "card_ticket",
    17: "realtime_location",
    19: "merged_messages",
    21: "mini_program_promo",
    24: "note",
    33: "mini_program",
    35: "message_history",
    36: "mini_program",
    40: "channel_forward",
    44: "channel_live_product",
    51: "channel_video",
    53: "group_chat_reference",
    57: "quote",
    62: "pat",
    63: "channel_live",
    74: "channel_files",
    87: "group_announcement",
    88: "group_note",
    92: "music",
    100: "sticker_set",
    101: "ad",
    107: "open_link",
    113: "video_account_intro",
    116: "channel_show_card",
    117: "channel_product",
    124: "wechat_gift",
    2000: "transfer",
    2001: "red_envelope",
    2003: "red_envelope_cover",
}

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_ENTITY = (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"))


def split_local_type(local_type: int) -> tuple[int, int]:
    msg_type = local_type & 0xFFFFFFFF
    sub_type = (local_type >> 32) & 0xFFFFFFFF
    return msg_type, sub_type


def type_label(msg_type: int) -> str:
    return TYPE_LABELS.get(msg_type, "unknown")


def subtype_label(msg_type: int, sub_type: int) -> str:
    if msg_type != MSG_TYPE_APP:
        return type_label(msg_type)
    return SUBTYPE_LABELS.get(sub_type, "app")


def _unescape(text: str) -> str:
    if "&" not in text:
        return text
    for entity, char in _ENTITY:
        text = text.replace(entity, char)
    return text


def decode_bytes(raw: bytes | str, wcdb_ct: int | None) -> str:
    """Decompress (when needed) and stringify one content column."""

    if not raw:
        return ''
    if isinstance(raw, str):
        return raw
    is_zstd = wcdb_ct == 4 or raw[:4] == ZSTD_MAGIC
    if is_zstd:
        try:
            raw = zstandard.ZstdDecompressor().decompress(raw, max_output_size=64 * 1024 * 1024)
        except zstandard.ZstdError:
            try:
                raw = zstandard.ZstdDecompressor().stream_reader(raw).read()
            except Exception:
                return raw.decode("utf-8", "replace")
    return raw.decode("utf-8", "replace")


def extract_tag(xml: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
    if not match:
        return None
    value = match.group(1)
    cdata = re.match(r"<!\[CDATA\[(.*?)\]\]>$", value, re.S)
    if cdata:
        value = cdata.group(1)
    value = _unescape(value)
    return value or None


def _display(value: str | None) -> str | None:
    if value is None or value == "null":
        return None
    return value


def parse_app(xml: str, sub_type: int) -> tuple[str, str]:
    """Return (display text, decode state) for an app/rich message."""

    title = _display(extract_tag(xml, "title"))
    des = _display(extract_tag(xml, "des"))
    label = subtype_label(MSG_TYPE_APP, sub_type)

    if sub_type == 57:  # quote / reply
        refer = re.search(r"<refermsg>(.*?)</refermsg>", xml, re.S)
        refer_text = extract_tag(refer.group(1), "content") if refer else None
        if refer_text and "<" in refer_text:
            inner_title = extract_tag(refer_text, "title")
            refer_text = inner_title or "[非文本内容]"
        parts = [p for p in (title, refer_text) if p]
        return " ｜ ".join(parts) or f"[{label}]", "decoded"

    if label == "file":
        file_ext = extract_tag(xml, "fileext")
        size = extract_tag(xml, "totallen")
        suffix = f".{file_ext}" if file_ext else ""
        size_text = f" ({int(size) / 1024:.0f} KB)" if size and size.isdigit() else ""
        return f"[文件] {title or '未命名'}{suffix}{size_text}", "decoded"

    if label == "transfer":
        amount = extract_tag(xml, "feedesc")
        memo = extract_tag(xml, "pay_memo")
        text = " ".join(part for part in (amount, memo) if part)
        return f"[转账] {text}".strip(), "decoded"

    if label == "red_envelope":
        return f"[红包] {title}" if title else "[红包]", "decoded"

    if label in {"merged_messages", "chat_log_backup", "chat_log_migrate"}:
        return f"[聊天记录] {title or ''}".strip(), "decoded"

    if label in {"mini_program", "mini_program_promo"}:
        return f"[小程序] {title or ''}".strip(), "decoded"

    if label in {"channel_video", "channel_live", "channel_forward", "channel_files", "channel_product"}:
        return f"[视频号] {title or ''}".strip(), "decoded"

    if label in {"music"}:
        return f"[音乐] {title or ''}".strip(), "decoded"

    if label in {"sticker_set", "gif"}:
        return f"[表情] {title or ''}".strip(), "decoded"

    if label == "link":
        pieces = [title, des]
        return " - ".join(p for p in pieces if p) or f"[链接] {extract_tag(xml, 'url') or ''}", "decoded"

    if title:
        return title, "decoded"
    if des:
        return des, "decoded"
    return f"[{label}]", "decoded"


def parse_message(
    msg_type: int,
    sub_type: int,
    content: str,
    *,
    is_group: bool,
    fallback_sender: str,
) -> tuple[str, str, str]:
    """Return (text, sender hint, decode state) for one message row."""

    sender = fallback_sender
    if is_group and ":\n" in content:
        head, rest = content.split(":\n", 1)
        if head and len(head) < 128 and "<" not in head:
            sender, content = head, rest

    placeholder = {
        MSG_TYPE_IMAGE: "[图片]",
        MSG_TYPE_VOICE: "[语音]",
        MSG_TYPE_VIDEO: "[视频]",
        MSG_TYPE_EMOJI: "[表情]",
        MSG_TYPE_LOCATION: "[位置]",
    }

    if msg_type == MSG_TYPE_TEXT:
        return content, sender, "decoded"
    if msg_type == MSG_TYPE_APP:
        text, state = parse_app(content, sub_type)
        return text, sender, state
    if msg_type == MSG_TYPE_REVOKE:
        return content or "[撤回了一条消息]", sender, "decoded"
    if msg_type == MSG_TYPE_SYSTEM:
        sys_text = _system_text(content)
        return sys_text, sender, "decoded"
    if msg_type in placeholder:
        md5 = extract_tag(content, "md5") if "<" in content else None
        label = placeholder[msg_type]
        if md5:
            return f"{label} md5={md5}", sender, "decoded"
        return label, sender, "decoded"
    if msg_type == MSG_TYPE_LOCATION:
        return "[位置]", sender, "decoded"

    return content[:200] or f"[{type_label(msg_type)}]", sender, "raw"


def _system_text(content: str) -> str:
    """Pull readable text out of a sysmsg XML, else keep the plain text."""

    if "<" not in content:
        return content
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return content
    for node in root.iter():
        if node.tag.lower() in {"text", "content", "replacemsg", "displayname"} and node.text:
            return node.text.strip()
    return content
