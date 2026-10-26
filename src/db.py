"""MySQL 访问层（PyMySQL）。

几个 MySQL 专属注意点，都写在代码里了：
1. 幂等插入用 `ON DUPLICATE KEY UPDATE seq = seq`，不用 INSERT IGNORE ——
   IGNORE 会把截断、超长这类真错误一起吞掉，排查时死无对证。
2. 媒体抢占用 `FOR UPDATE SKIP LOCKED`（MySQL 8 才有），多 worker 不会互相等。
3. 中文检索走 ngram 全文索引，`MATCH ... AGAINST`；索引没建好或关键词
   短于 ngram_token_size 时降级 LIKE。
4. 单条 raw 可能很大，连接上把 max_allowed_packet 抬到 64MB，服务端也要同步改。
"""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import pymysql
from pymysql.cursors import DictCursor

from .normalize import MsgRow

MSG_COLS = (
    "seq", "msg_id", "action", "from_user", "room_id",
    "msg_type", "msg_time", "content", "raw", "media_count",
)
TO_COLS = ("seq", "user_id")
MEDIA_COLS = (
    "seq", "msg_id", "msg_time", "kind", "file_id", "file_id_hash", "filename",
    "file_md5", "file_size", "deadline_at",
)


class DB:
    """PyMySQL 连接不是线程安全的，所以每个线程一份连接（thread-local）。

    media worker 用线程池并发下载，共用一条连接必然 InterfaceError(0, '')
    —— 这个坑踩过一次就够了。
    """

    def __init__(self, cfg):
        self._cfg = cfg
        self._local = threading.local()
        self._all: list = []
        self._lock = threading.Lock()

    # ---------------- 连接 ----------------
    def _new_conn(self) -> pymysql.connections.Connection:
        return pymysql.connect(
            host=self._cfg.mysql.host,
            port=self._cfg.mysql.port,
            user=self._cfg.mysql.user,
            password=self._cfg.mysql.password,
            database=self._cfg.mysql.database,
            charset=self._cfg.mysql.charset,
            cursorclass=DictCursor,
            autocommit=False,
            max_allowed_packet=self._cfg.mysql.max_allowed_packet,
            local_infile=False,
            init_command="SET time_zone = '%+03d:00'" % self._cfg.timezone_offset_hours,
        )

    def connect(self) -> "DB":
        self.conn  # noqa: B018  触发建连，连不上就早失败
        return self

    @property
    def conn(self) -> pymysql.connections.Connection:
        c = getattr(self._local, "conn", None)
        if c is not None:
            try:
                c.ping(reconnect=not getattr(self._local, "collector_lock", False))
                return c
            except Exception:
                if getattr(self._local, "collector_lock", False):
                    raise RuntimeError("collector database connection lost; restart to reacquire lock")
                with contextlib.suppress(Exception):
                    c.close()
        c = self._new_conn()
        self._local.conn = c
        with self._lock:
            self._all.append(c)
        return c

    @contextlib.contextmanager
    def tx(self) -> Iterator[DictCursor]:
        conn = self.conn
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise
        finally:
            cur.close()

    def close(self) -> None:
        with self._lock:
            conns, self._all = self._all, []
        for c in conns:
            with contextlib.suppress(Exception):
                c.close()
        self._local = threading.local()

    def acquire_collector_lock(self) -> bool:
        # Kept on the main connection until close. DB failure must stop/retry
        # the process; reconnecting transparently would lose this lock.
        with self.tx() as c:
            c.execute("SELECT GET_LOCK(%s, 0) AS acquired", ("chatdata:" + self._cfg.mysql.database,))
            ok = c.fetchone()["acquired"] == 1
        if ok:
            self._local.collector_lock = True
        return ok

    # ---------------- 建表 ----------------
    def init_schema(self, schema_file: Path | str) -> None:
        import re

        sql = Path(schema_file).read_text(encoding="utf-8")
        # 先剥掉整行注释再切分，否则 "-- xxx\nCREATE TABLE ..." 这种块会被误判成注释丢掉
        sql = re.sub(r"(?m)^\s*--.*$", "", sql)
        stmts = [s.strip() for s in sql.split(";") if s.strip()]
        # 建库语句要单独、且不能在已选库的连接里跑
        cfg = self._cfg.mysql
        boot = pymysql.connect(
            host=cfg.host, port=cfg.port, user=cfg.user, password=cfg.password,
            charset="utf8mb4", autocommit=True,
        )
        try:
            with boot.cursor() as c:
                # 库名以 config 为准，schema.sql 里的 CREATE DATABASE 只作模板
                c.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{cfg.database}` "
                    "DEFAULT CHARACTER SET utf8mb4 "
                    "DEFAULT COLLATE utf8mb4_0900_ai_ci"
                )
        finally:
            boot.close()
        with self.tx() as c:
            for s in stmts:
                head = s.lstrip().upper()
                if head.startswith("CREATE DATABASE"):
                    continue
                if head.startswith("USE "):
                    continue
                c.execute(s)

    # ---------------- 游标 ----------------
    def get_last_seq(self) -> int:
        with self.tx() as c:
            c.execute("SELECT last_seq FROM pull_cursor WHERE id = 1")
            row = c.fetchone()
            return int(row["last_seq"]) if row else 0

    # ---------------- 落库（单事务，游标与消息同生共死） ----------------
    def save_batch(self, msgs: Sequence[MsgRow], new_seq: int) -> dict[str, int]:
        if not msgs:
            return {"messages": 0, "media": 0, "rooms": 0}
        inc = "(" + ",".join(["%s"] * len(MSG_COLS)) + ")"
        upd = ",".join(f"`{c}`=`{c}`" for c in ("msg_id",))
        msg_sql = (
            f"INSERT INTO chat_message ({','.join('`%s`' % c for c in MSG_COLS)}) "
            f"VALUES {inc} ON DUPLICATE KEY UPDATE {upd}"
        )
        msg_rows: list[tuple] = []
        to_rows: list[tuple] = []
        media_rows: list[tuple] = []
        rooms: dict[str, Any] = {}

        for m in msgs:
            msg_rows.append((
                m.seq, m.msg_id, m.action, m.from_user, m.room_id,
                m.msg_type, m.msg_time, m.content, m.raw, len(m.media),
            ))
            for u in m.tolist:
                to_rows.append((m.seq, u))
            for md in m.media:
                media_rows.append((
                    md.seq, md.msg_id, md.msg_time, md.kind, md.file_id, md.file_id_hash,
                    md.filename, md.file_md5, md.file_size, md.deadline_at,
                ))
            if m.room_id:
                rooms[m.room_id] = max(rooms.get(m.room_id, m.msg_time), m.msg_time)

        with self.tx() as c:
            c.executemany(msg_sql, msg_rows)
            if to_rows:
                c.executemany(
                    "INSERT INTO chat_message_to (seq, user_id) VALUES (%s, %s) "
                    "ON DUPLICATE KEY UPDATE `seq`=`seq`",
                    to_rows,
                )
            if media_rows:
                cols = ",".join("`%s`" % x for x in MEDIA_COLS)
                ph = "(" + ",".join(["%s"] * len(MEDIA_COLS)) + ")"
                c.executemany(
                    f"INSERT INTO media_object ({cols}) VALUES {ph} "
                    "ON DUPLICATE KEY UPDATE `file_id_hash`=`file_id_hash`",
                    media_rows,
                )
            if rooms:
                # 单条语句搞定「首次插入 / 之后累加」，避免先查后写
                c.executemany(
                    "INSERT INTO chat_room (room_id, last_msg_time, msg_count) "
                    "VALUES (%s, %s, 1) ON DUPLICATE KEY UPDATE "
                    "  msg_count = msg_count + 1, "
                    "  last_msg_time = GREATEST(COALESCE(last_msg_time, VALUES(last_msg_time)), "
                    "                            VALUES(last_msg_time))",
                    list(rooms.items()),
                )
            c.execute(
                "UPDATE pull_cursor SET last_seq = %s WHERE id = 1 AND last_seq < %s",
                (new_seq, new_seq),
            )
        return {"messages": len(msg_rows), "media": len(media_rows), "rooms": len(rooms)}

    # ---------------- 媒体队列 ----------------
    def claim_media(self, n: int) -> list[dict]:
        with self.tx() as c:
            c.execute(
                "SELECT id, seq, msg_id, msg_time, kind, file_id, filename, "
                "       deadline_at, attempts, file_md5, file_size "
                "FROM media_object "
                "WHERE status = 'pending' "
                "  AND deadline_at > NOW(3) "
                "  AND (next_retry_at IS NULL OR next_retry_at <= NOW(3)) "
                "ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED",
                (n,),
            )
            rows = list(c.fetchall())
            if not rows:
                return []
            ids = [r["id"] for r in rows]
            c.executemany(
                "UPDATE media_object SET status='downloading', attempts=attempts+1 "
                "WHERE id=%s",
                [(i,) for i in ids],
            )
        return rows

    def expire_stale_media(self) -> int:
        with self.tx() as c:
            return c.execute(
                "UPDATE media_object SET status='expired', "
                "last_error=CONCAT('sdkfileid 已过有效期，deadline=', deadline_at) "
                "WHERE status='pending' AND deadline_at <= NOW(3)"
            )

    def reset_stuck_media(self, minutes: int = 30) -> int:
        """进程被杀时留下的 'downloading' 行，超时后放回队列，否则永远卡住。"""
        with self.tx() as c:
            return c.execute(
                "UPDATE media_object SET status='pending', "
                "last_error='worker 超时回收' "
                "WHERE status='downloading' "
                "  AND updated_at < DATE_SUB(NOW(3), INTERVAL %s MINUTE)",
                (minutes,),
            )

    def finish_media(self, media_id: int, storage_key: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE media_object SET status='done', storage_key=%s, "
                "last_error=NULL, next_retry_at=NULL WHERE id=%s",
                (storage_key, media_id),
            )

    def fail_media(self, media_id: int, attempts: int, err: str,
                   backoff_seconds: int, max_attempts: int) -> None:
        final = attempts >= max_attempts
        with self.tx() as c:
            c.execute(
                "UPDATE media_object SET status=%s, last_error=%s, "
                "next_retry_at=DATE_ADD(NOW(3), INTERVAL %s SECOND) WHERE id=%s",
                ("failed" if final else "pending", err[:500],
                 backoff_seconds, media_id),
            )

    def mark_media_expired(self, media_id: int, err: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE media_object SET status='expired', last_error=%s WHERE id=%s",
                (err[:500], media_id),
            )

    # ---------------- 查询 ----------------
    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        with self.tx() as c:
            c.execute("SELECT last_seq, updated_at FROM pull_cursor WHERE id=1")
            out["cursor"] = c.fetchone()
            c.execute("SELECT COUNT(*) n, MIN(msg_time) a, MAX(msg_time) b FROM chat_message")
            out["messages"] = c.fetchone()
            c.execute("SELECT status, COUNT(*) n FROM media_object GROUP BY status")
            out["media"] = {r["status"]: r["n"] for r in c.fetchall()}
            c.execute("SELECT msg_type, COUNT(*) n FROM chat_message "
                      "GROUP BY msg_type ORDER BY n DESC")
            out["by_type"] = c.fetchall()
            c.execute("SELECT COUNT(*) n FROM chat_room")
            out["rooms"] = c.fetchone()["n"]
        return out

    def search(self, kw: str, limit: int = 20) -> list[dict]:
        with self.tx() as c:
            try:
                c.execute(
                    "SELECT seq, msg_time, from_user, room_id, msg_type, "
                    "LEFT(content, 200) AS snippet "
                    "FROM chat_message WHERE MATCH(content) AGAINST (%s IN BOOLEAN MODE) "
                    "ORDER BY msg_time DESC LIMIT %s",
                    (kw, limit),
                )
            except pymysql.err.MySQLError:
                c.execute(
                    "SELECT seq, msg_time, from_user, room_id, msg_type, "
                    "LEFT(content, 200) AS snippet FROM chat_message "
                    "WHERE content LIKE %s ORDER BY msg_time DESC LIMIT %s",
                    (f"%{kw}%", limit),
                )
            return list(c.fetchall())
