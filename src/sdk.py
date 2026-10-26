"""libWeWorkFinanceSdk 的 ctypes 封装 + 离线 FakeSDK。

真实 SDK 的 C 接口（与官方 C SDK 头文件一致）：

    long  NewSdk();
    int   Init(long sdk, const char* corpid, const char* secret);
    int   GetChatData(long sdk, unsigned long long seq, unsigned int limit,
                      const char* proxy, const char* passwd, int timeout,
                      Slice_t* chatdatas);
    int   DecryptData(const char* encrypt_key, const char* encrypt_msg, Slice_t* msg);
    int   GetMediaData(long sdk, const char* indexbuf, const char* sdkfileid,
                       const char* proxy, const char* passwd, int timeout,
                       MediaData_t* media_data);
    void  FreeSlice(Slice_t* slice);
    void  DestroySdk(long sdk);

注意：Slice 里的 buf / MediaData 里的 data 都用 c_void_p 接，再用 string_at
按长度取。用 c_char_p 会被 NUL 截断 —— 媒体二进制里 NUL 遍地都是，直接坏文件。
"""

from __future__ import annotations

import base64
import ctypes
import json
import random
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

# 官方 SDK 各平台的库文件名，按顺序找
_LIB_CANDIDATES = (
    "libWeWorkFinanceSdk.so",
    "libWeWorkFinanceSdk_C.so",
    "libWeWorkFinanceSdk.dylib",
    "WeWorkFinanceSdk.dll",
)


class SdkError(RuntimeError):
    def __init__(self, code: int, msg: str):
        super().__init__(f"errcode={code} errmsg={msg}")
        self.code = code
        self.msg = msg


class MediaExpired(SdkError):
    """媒体已明确失效（模拟 SDK 使用）。"""

    def __init__(self, code: int = -1, msg: str = "media expired"):
        super().__init__(code, msg)


@dataclass
class ChatItem:
    seq: int
    msgid: str
    publickey_ver: int
    encrypt_random_key: str
    encrypt_chat_msg: str


class ChatSdk(Protocol):
    def get_chat_data(self, seq: int, limit: int) -> list[ChatItem]: ...
    def decrypt(self, random_key: str, encrypt_msg: str, publickey_ver: int = 1) -> dict: ...
    def get_media(self, sdkfileid: str) -> Iterator[bytes]: ...
    def close(self) -> None: ...


# --------------------------------------------------------------------------
# ctypes 结构
# --------------------------------------------------------------------------
class _Slice(ctypes.Structure):
    _fields_ = [("buf", ctypes.c_void_p), ("len", ctypes.c_int)]


class _MediaData(ctypes.Structure):
    _fields_ = [
        ("outindexbuf", ctypes.c_void_p),
        ("out_len", ctypes.c_int),
        ("data", ctypes.c_void_p),
        ("data_len", ctypes.c_int),
        ("is_finish", ctypes.c_int),
    ]


def _find_library(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        if not p.exists():
            raise FileNotFoundError(f"指定的 SDK 动态库不存在：{p}")
        return p
    here = Path(__file__).resolve().parent.parent
    for d in (here / "lib", here, Path.cwd()):
        for name in _LIB_CANDIDATES:
            if (d / name).exists():
                return d / name
    raise FileNotFoundError(
        "找不到会话存档 SDK 动态库。把官方 SDK 里的 "
        f"{'/'.join(_LIB_CANDIDATES)} 放到 {here / 'lib'} 下，"
        "或在 config.yaml 里设 wecom.sdk_lib_path。"
    )


class WeWorkFinanceSdk:
    """真实 SDK 封装。一个实例 = 一个 SDK 句柄，别跨线程共享。"""

    def __init__(
        self,
        corpid: str,
        secret: str,
        private_key: str,
        lib_path: str | None = None,
        proxy: str = "",
        proxy_password: str = "",
        timeout: int = 30,
        private_keys: dict | None = None,
    ):
        self._lib = ctypes.CDLL(str(_find_library(lib_path)))
        self._decl()
        self._proxy = proxy.encode()
        self._passwd = proxy_password.encode()
        self._timeout = timeout
        self._priv = private_key.encode()
        self._keys = private_keys or {}
        self._sdk = self._lib.NewSdk()
        if not self._sdk:
            raise SdkError(-1, "NewSdk() 返回 0")
        ret = self._lib.Init(self._sdk, corpid.encode(), secret.encode())
        if ret != 0:
            self.close()
            raise SdkError(ret, "Init 失败：检查 corpid / 会话存档 secret / 可信 IP")

    def _decl(self) -> None:
        lib = self._lib
        lib.NewSdk.restype = ctypes.c_void_p
        lib.Init.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        lib.GetChatData.argtypes = [
            ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_uint,
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int,
            ctypes.POINTER(_Slice),
        ]
        lib.DecryptData.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(_Slice),
        ]
        lib.GetMediaData.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int,
            ctypes.POINTER(_MediaData),
        ]
        lib.NewSlice.restype = ctypes.POINTER(_Slice)
        lib.FreeSlice.argtypes = [ctypes.POINTER(_Slice)]
        lib.NewMediaData.restype = ctypes.POINTER(_MediaData)
        lib.FreeMediaData.argtypes = [ctypes.POINTER(_MediaData)]
        lib.DestroySdk.argtypes = [ctypes.c_void_p]

    def get_chat_data(self, seq: int, limit: int) -> list[ChatItem]:
        holder = self._lib.NewSlice()
        try:
            ret = self._lib.GetChatData(
                self._sdk, seq, limit, self._proxy, self._passwd,
                self._timeout, holder,
            )
            if ret != 0:
                raise SdkError(ret, "GetChatData 失败")
            raw = ctypes.string_at(holder.contents.buf, holder.contents.len)
        finally:
            self._lib.FreeSlice(holder)
        if not raw:
            return []
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("errcode", 0) != 0:
            raise SdkError(payload["errcode"], payload.get("errmsg", ""))
        out: list[ChatItem] = []
        for it in payload.get("chatdata", []):
            out.append(
                ChatItem(
                    seq=int(it["seq"]),
                    msgid=it.get("msgid", ""),
                    publickey_ver=int(it.get("publickey_ver", 1)),
                    encrypt_random_key=it["encrypt_random_key"],
                    encrypt_chat_msg=it["encrypt_chat_msg"],
                )
            )
        return out

    def decrypt(self, random_key: str, encrypt_msg: str, publickey_ver: int = 1) -> dict:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from cryptography.hazmat.primitives.asymmetric import padding
        pem = self._keys.get(str(publickey_ver)) if self._keys else self._priv
        if not pem:
            raise SdkError(10007, "no private key for publickey_ver")
        if isinstance(pem, str):
            pem = pem.encode()
        key = load_pem_private_key(pem, password=None)
        decrypted_key = key.decrypt(base64.b64decode(random_key, validate=True), padding.PKCS1v15())
        holder = self._lib.NewSlice()
        try:
            ret = self._lib.DecryptData(decrypted_key, encrypt_msg.encode(), holder)
            if ret != 0:
                raise SdkError(ret, "DecryptData 失败：私钥与后台公钥不匹配？")
            raw = ctypes.string_at(holder.contents.buf, holder.contents.len)
        finally:
            self._lib.FreeSlice(holder)
        return json.loads(raw.decode("utf-8"))

    def get_media(self, sdkfileid: str) -> Iterator[bytes]:
        """分片拉取媒体，indexbuf 为空开始，is_finish 为 1 结束。"""
        indexbuf = b""
        while True:
            holder = self._lib.NewMediaData()
            try:
                ret = self._lib.GetMediaData(
                    self._sdk, indexbuf or None, sdkfileid.encode(),
                    self._proxy, self._passwd, self._timeout, holder,
                )
                if ret != 0:
                    raise SdkError(ret, "GetMediaData failed; retry or investigate")
                md = holder.contents
                chunk = ctypes.string_at(md.data, md.data_len) if md.data_len else b""
                finished = bool(md.is_finish)
                next_index = ctypes.string_at(md.outindexbuf, md.out_len) if md.outindexbuf else b""
            finally:
                self._lib.FreeMediaData(holder)
            if chunk:
                yield chunk
            if finished:
                return
            if not next_index or next_index == indexbuf:
                raise SdkError(-1, "incomplete media: continuation cursor did not advance")
            indexbuf = next_index

    def close(self) -> None:
        if getattr(self, "_sdk", None):
            self._lib.DestroySdk(self._sdk)
            self._sdk = 0


# --------------------------------------------------------------------------
# FakeSDK：不连企微、不需要 license，用来验证库表和整条流水线
# --------------------------------------------------------------------------
class FakeSdk:
    """生成可预测的合成消息。encrypt_chat_msg 就是明文 JSON，解密直接返回。"""

    _TYPES = ("text", "text", "text", "image", "file", "link", "voice", "revoke")

    def __init__(self, total: int = 12, seed: int = 7):
        self._buf: list[ChatItem] = []
        self._plain: dict[str, dict] = {}
        rnd = random.Random(seed)
        now_ms = int(time.time() * 1000)
        users = ["zhangsan", "lisi", "wangwu"]
        rooms = ["wrOgQhDgAAxxxxxx", "wrOgQhDgAAyyyyyy"]
        # 一条故意做成 3 天前的媒体，用来跑「已过期」分支
        stale_ms = now_ms - 4 * 24 * 3600 * 1000
        for i in range(1, total + 1):
            mtype = self._TYPES[i % len(self._TYPES)]
            msgid = f"fake-{i:05d}-{uuid.uuid4().hex[:8]}"
            room = rooms[i % 2] if i % 3 else None
            msgtime = now_ms - (total - i) * 60_000
            plain: dict = {
                "msgid": msgid,
                "action": "recall" if mtype == "revoke" else "send",
                "from": users[i % len(users)],
                "tolist": [users[(i + 1) % len(users)]],
                "roomid": room,
                "msgtime": msgtime,
                "msgtype": mtype,
            }
            if mtype == "text":
                plain["text"] = {"content": f"第 {i} 条测试消息：报价单什么时候发？"}
            elif mtype == "image":
                plain["image"] = {
                    "sdkfileid": f"FILEID_IMG_{i}_" + "x" * 40,
                    "md5sum": __import__("hashlib").md5(b"x" * (181 * 1024)).hexdigest(),
                    "filesize": 181 * 1024,
                }
            elif mtype == "file":
                plain["file"] = {
                    "sdkfileid": f"FILEID_DOC_{i}_" + "y" * 40,
                    "filename": f"合同-{i}.pdf",
                    "filesize": 2 * 1024 * 1024,
                }
            elif mtype == "link":
                plain["link"] = {
                    "title": f"行业报告 {i}",
                    "description": "2026 年渠道政策解读",
                    "link_url": f"https://example.com/r/{i}",
                }
            elif mtype == "voice":
                plain["voice"] = {
                    "sdkfileid": f"FILEID_VOICE_STALE_{i}",
                    "md5sum": "1" * 32,
                    "voice_size": 8 * 1024,
                    "play_length": 3200,
                }
                plain["msgtime"] = stale_ms
            elif mtype == "revoke":
                plain["revoke"] = {"pre_msgid": f"fake-{i - 1:05d}"}
                plain["msgtype"] = "revoke"
            self._plain[msgid] = plain
            self._buf.append(
                ChatItem(
                    seq=i,
                    msgid=msgid,
                    publickey_ver=1,
                    encrypt_random_key="FAKE_RANDOM_KEY",
                    encrypt_chat_msg=json.dumps(plain, ensure_ascii=False),
                )
            )

    def get_chat_data(self, seq: int, limit: int) -> list[ChatItem]:
        return [c for c in self._buf if c.seq > seq][:limit]

    def decrypt(self, random_key: str, encrypt_msg: str, publickey_ver: int = 1) -> dict:
        return json.loads(encrypt_msg)

    def get_media(self, sdkfileid: str) -> Iterator[bytes]:
        if "STALE" in sdkfileid:
            raise MediaExpired(-1, "fake: 超过 3 天有效期")
        total = 181 * 1024 if "IMG" in sdkfileid else 2 * 1024 * 1024
        while total:
            chunk_size = min(total, 4096)
            yield b"x" * chunk_size
            total -= chunk_size

    def close(self) -> None:
        pass


def build(cfg, fake: bool | None = None) -> ChatSdk:
    if fake or cfg.mode == "fake":
        return FakeSdk()
    return WeWorkFinanceSdk(
        corpid=cfg.wecom.corpid,
        secret=cfg.wecom.secret,
        private_key=cfg.private_key() if not cfg.wecom.private_key_paths else "",
        private_keys={str(v): (Path(path) if Path(path).is_absolute() else Path(__file__).resolve().parent.parent / path).read_text() for v, path in cfg.wecom.private_key_paths.items()},
        lib_path=cfg.wecom.sdk_lib_path or None,
        proxy=cfg.wecom.proxy,
        proxy_password=cfg.wecom.proxy_password,
        timeout=cfg.wecom.timeout_seconds,
    )
