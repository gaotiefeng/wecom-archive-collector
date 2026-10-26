"""拉取主循环。

seq 只能严格往前走，所以这里是单线程顺序拉 —— 别想着多线程并发 GetChatData，
序号会互相穿越，游标就废了。要提速只能调大 batch_limit。
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime

from . import normalize as norm
from .db import DB
from .sdk import SdkError

log = logging.getLogger("chatdata.puller")


class Puller:
    def __init__(self, cfg, db: DB, sdk):
        self.cfg = cfg
        self.db = db
        self.sdk = sdk
        self._stop = False

    def _decrypt_one(self, item) -> norm.MsgRow:
        try:
            plain = self.sdk.decrypt(item.encrypt_random_key, item.encrypt_chat_msg, item.publickey_ver)
            return norm.normalize(
                plain,
                seq=item.seq,
                fallback_msgid=item.msgid,
                offset_hours=self.cfg.timezone_offset_hours,
                ttl_hours=self.cfg.media.ttl_hours,
            )
        except Exception:
            # Fail closed: retain the cursor so the encrypted record can be fetched
            # again after correcting the key/configuration. Never save an empty stub.
            log.error("message processing failed seq=%s; cursor unchanged", item.seq)
            raise

    def pull_batch(self) -> int:
        """拉一批并落库，返回落库条数。"""
        limit = self.cfg.wecom.batch_limit
        last = self.db.get_last_seq()
        items = self.sdk.get_chat_data(last, limit)
        if not items:
            return 0

        items = [i for i in items if i.seq > last]
        if not items:
            return 0

        msgs = [self._decrypt_one(i) for i in items]
        new_seq = max(i.seq for i in items)
        stat = self.db.save_batch(msgs, new_seq)
        log.info(
            "batch ok last=%s -> %s msgs=%s media=%s rooms=%s",
            last, new_seq, stat["messages"], stat["media"], stat["rooms"],
        )
        return len(msgs)

    def run_until_idle(self) -> int:
        total = 0
        for _ in range(self.cfg.pull.max_batches_per_round):
            n = self.pull_batch()
            if n == 0:
                break
            total += n
        return total

    def stop(self, *_a) -> None:
        self._stop = True
        log.info("收到停止信号，本轮结束后退出")

    def run_forever(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        backoff = 1
        while not self._stop:
            try:
                n = self.run_until_idle()
                backoff = 1
                if n == 0:
                    time.sleep(self.cfg.pull.poll_interval_seconds)
            except SdkError as e:
                # 10002/45009 之类限频或临时故障，指数退避
                log.error("SDK 错误 %s，%ss 后重试", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
            except Exception as e:  # noqa: BLE001
                log.exception("未预期错误 %r，%ss 后重试", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
        log.info("puller 退出")
