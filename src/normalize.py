"""把解密后的消息 JSON 归一化成三张表的行。

原则：raw 原样保留，任何新 msgtype 都不会丢数据；content 只做「能被人搜到」
的可读摘要，不承担结构化职责。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# 媒体可在 3 天内下载，这里保守留 1 小时余量由 media.ttl_hours 控制
EMOJI = {
    "text": "[文本]",
    "image": "[图片]",
    "voice": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "link": "[链接]",
    "emotion": "[表情]",
    "location": "[位置]",
    "card": "[名片]",
    "redpacket": "[红包]",
    "external_redpacket": "[红包]",
    "weapp": "[小程序]",
    "chatrecord": "[聊天记录]",
    "docmsg": "[文档]",
    "markdown": "[Markdown]",
    "news": "[图文]",
    "calendar": "[日程]",
    "meeting": "[会议]",
    "meeting_voice_call": "[语音通话]",
    "voip_doc_share": "[通话共享文档]",
    "mixed": "[混合消息]",
    "agree": "[同意]",
    "disagree": "[不同意]",
    "collect": "[接龙]",
    "sphfeed": "[视频号]",
    "todo": "[待办]",
    "vote": "[投票]",
    "revoke": "[撤回]",
    "switch": "[切换会话]",
}


def ms_to_dt(ms: int, offset_hours: int = 8) -> datetime:
    """企微 msgtime 是毫秒时间戳 → 指定固定偏移的墙钟时间（naive）。

    不用 zoneinfo 是为了跟 MySQL 的 DATETIME 无时区语义严格对齐，
    也不想依赖 MySQL 有没有加载时区表。
    """
    tz = timezone(timedelta(hours=offset_hours))
    return datetime.fromtimestamp(ms / 1000.0, tz=tz).replace(tzinfo=None)


def _digest(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@dataclass
class MediaRow:
    seq: int
    msg_id: str
    msg_time: datetime
    kind: str
    file_id: str
    filename: str | None
    file_md5: str | None
    file_size: int | None
    deadline_at: datetime

    @property
    def file_id_hash(self) -> str:
        return _digest(self.file_id)


@dataclass
class MsgRow:
    seq: int
    msg_id: str
    action: str
    from_user: str
    room_id: str | None
    msg_type: str
    msg_time: datetime
    content: str | None
    raw: str
    tolist: list[str] = field(default_factory=list)
    media: list[MediaRow] = field(default_factory=list)


def _text_of(plain: dict) -> str:
    t = plain.get("msgtype", "unknown")
    body = plain.get(t) or {}

    if t == "text":
        return body.get("content", "")
    if t == "markdown":
        return body.get("content", "")
    if t == "link":
        parts = [body.get("title"), body.get("description"), body.get("link_url")]
        return " ".join(p for p in parts if p)
    if t == "location":
        parts = [body.get("title"), body.get("address"), body.get("poiname")]
        return " ".join(p for p in parts if p)
    if t == "card":
        return " ".join(
            str(p) for p in (body.get("corpname"), body.get("userid")) if p
        )
    if t == "file":
        return body.get("filename", "")
    if t == "docmsg":
        return body.get("title") or body.get("doc_name") or ""
    if t == "news":
        return (body.get("info") or {}).get("title", "") if isinstance(body.get("info"), dict) else ""
    if t == "chatrecord":
        items = (body.get("item") or [])
        texts = []
        for it in items:
            for k, v in (it or {}).items():
                if isinstance(v, dict) and v.get("content"):
                    texts.append(str(v["content"]))
        return " | ".join(texts)
    if t == "mixed":
        texts = []
        for it in body.get("item") or []:
            sub = it.get("text") if isinstance(it, dict) else None
            if isinstance(sub, dict) and sub.get("content"):
                texts.append(sub["content"])
        return " ".join(texts)
    if t == "agree":
        return "同意"
    if t == "disagree":
        return "不同意"
    if t == "revoke":
        return "撤回了一条消息"
    if t == "switch":
        return "切换了会话"
    return EMOJI.get(t, f"[{t}]")


def _media_of(plain: dict, seq: int, msg_id: str, msg_time: datetime,
              ttl_hours: int) -> list[MediaRow]:
    t = plain.get("msgtype")
    body = plain.get(t) or {}
    deadline = msg_time + timedelta(hours=ttl_hours) if ttl_hours > 0 else datetime(9999, 1, 1)
    out: list[MediaRow] = []

    def add(kind: str, fid: str | None, filename=None, md5=None, size=None):
        if not fid:
            return
        out.append(
            MediaRow(
                seq=seq, msg_id=msg_id, msg_time=msg_time, kind=kind, file_id=fid,
                filename=filename, file_md5=md5, file_size=size,
                deadline_at=deadline,
            )
        )

    seen = set()
    def visit(node, kind="file"):
        if isinstance(node, list):
            for value in node:
                visit(value, kind)
        elif isinstance(node, dict):
            kind = str(node.get("type") or node.get("msgtype") or kind)
            fid = node.get("sdkfileid")
            if fid and fid not in seen:
                seen.add(fid)
                add(kind if kind in ("image", "emotion", "voice", "video", "file") else "file",
                    fid, node.get("filename"), node.get("md5sum"),
                    node.get("filesize", node.get("voice_size")))
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    visit(value, key if key in ("image", "emotion", "voice", "video", "file") else kind)
                elif key == "content" and isinstance(value, str):
                    try:
                        decoded = json.loads(value)
                    except (ValueError, TypeError):
                        continue
                    visit(decoded, kind)
    visit(body, t)
    return out


def normalize(
    plain: dict,
    seq: int,
    fallback_msgid: str,
    offset_hours: int = 8,
    ttl_hours: int = 71,
) -> MsgRow:
    msg_id = plain.get("msgid") or fallback_msgid
    msg_type = plain.get("msgtype") or "unknown"
    msg_time = ms_to_dt(int(plain.get("msgtime") or 0), offset_hours)
    action = plain.get("action") or "send"
    room = plain.get("roomid") or None

    return MsgRow(
        seq=seq,
        msg_id=msg_id,
        action=action,
        from_user=str(plain.get("from") or ""),
        room_id=room,
        msg_type=msg_type,
        msg_time=msg_time,
        content=_text_of(plain),
        raw=json.dumps(plain, ensure_ascii=False),
        tolist=[str(x) for x in (plain.get("tolist") or [])],
        media=_media_of(plain, seq, msg_id, msg_time, ttl_hours),
    )
