"""媒体下载队列：将 sdkfileid 对应媒体下载到自有存储。

为什么单独一个 worker：
- 拉消息是顺序单线程（seq 只能往前走），媒体下载是天然并发的；
- sdkfileid 过期就永久拿不回来，下载失败必须重试 + 落 expired 状态留痕，
  不能因为一个图片卡住消息拉取。
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from . import storage as storage_mod
from .db import DB
from .sdk import MediaExpired

log = logging.getLogger("chatdata.media")


class MediaWorker:
    def __init__(self, cfg, db: DB, store, sdk_factory):
        self.cfg = cfg
        self.db = db
        self.store = store
        self._sdk_factory = sdk_factory
        self._local = threading.local()
        self.done = 0
        self.failed = 0
        self.expired = 0
        self._handles = []
        self._handles_lock = threading.Lock()

    def _sdk(self):
        # SDK 句柄不是线程安全的，每个线程一份
        if not hasattr(self._local, "sdk"):
            self._local.sdk = self._sdk_factory()
            with self._handles_lock:
                self._handles.append(self._local.sdk)
        return self._local.sdk

    def _one(self, row: dict) -> None:
        mid = row["id"]
        attempts = int(row["attempts"]) + 1  # claim 时已经 +1
        try:
            key = storage_mod.build_key(
                row["msg_time"], row["msg_id"], f"{mid}_" + (row["filename"] or row["kind"])
            )
            def checked_chunks():
                import hashlib
                digest = hashlib.md5()
                total = 0
                for chunk in self._sdk().get_media(row["file_id"]):
                    digest.update(chunk)
                    total += len(chunk)
                    yield chunk
                expected_size = row.get("file_size")
                if expected_size is not None and total != int(expected_size):
                    raise ValueError("media size mismatch")
                expected_md5 = row.get("file_md5")
                if expected_md5 and digest.hexdigest().lower() != expected_md5.lower():
                    raise ValueError("media MD5 mismatch")
            key, md5, size = self.store.save(key, checked_chunks())
            self.db.finish_media(mid, key)
            self.done += 1
            log.info("media ok id=%s kind=%s size=%s key=%s", mid, row["kind"], size, key)
        except MediaExpired as e:
            self.db.mark_media_expired(mid, f"sdkfileid 失效：{e}")
            self.expired += 1
            log.warning("media expired id=%s seq=%s", mid, row["seq"])
        except Exception as e:  # noqa: BLE001
            backoff = min(30 * (2 ** max(0, attempts - 1)), 3600)
            self.db.fail_media(mid, attempts, repr(e), backoff,
                               self.cfg.media.max_attempts)
            self.failed += 1
            log.warning("media fail id=%s attempts=%s err=%r", mid, attempts, e)

    def drain(self, batch: int = 20) -> dict[str, int]:
        self.done = self.failed = self.expired = 0
        if not self.cfg.media.enabled:
            return {"done": 0, "failed": 0, "expired": 0}
        stuck = self.db.reset_stuck_media()
        if stuck:
            log.warning("回收 %s 个卡在 downloading 的任务", stuck)

        n = self.db.expire_stale_media()
        if n:
            self.expired += n
            log.warning("把 %s 个过期未下载的媒体标成 expired", n)

        try:
            with ThreadPoolExecutor(max_workers=max(1, self.cfg.media.workers)) as pool:
                while True:
                    rows = self.db.claim_media(batch)
                    if not rows:
                        break
                    list(pool.map(self._one, rows))
        finally:
            for handle in self._handles:
                handle.close()
            self._handles.clear()
        return {"done": self.done, "failed": self.failed, "expired": self.expired}
