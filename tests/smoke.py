"""端到端冒烟测试：不需要企微 license，用 FakeSdk 跑通「拉取 → 落库 → 媒体 → 检索」。

    python tests/smoke.py

会先 drop 掉 config.yaml 里配的那个库再重建 —— 只在自己的测试库上跑。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pymysql  # noqa: E402

from src import config as config_mod  # noqa: E402
from src import media as media_mod  # noqa: E402
from src import sdk as sdk_mod  # noqa: E402
from src import storage as storage_mod  # noqa: E402
from src.db import DB  # noqa: E402
from src.puller import Puller  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"{'  ok ' if cond else 'FAIL '} {name} {extra}")
    if not cond:
        FAILS.append(name)


def main() -> int:
    cfg = config_mod.load(os.environ.get("CHATDATA_CONFIG"))
    cfg.mode = "fake"
    cfg.mysql.database = cfg.mysql.database + "_test"
    cfg.storage.local_root = "./var/files_test"

    boot = pymysql.connect(
        host=cfg.mysql.host, port=cfg.mysql.port, user=cfg.mysql.user,
        password=cfg.mysql.password, autocommit=True,
    )
    with boot.cursor() as c:
        c.execute(f"DROP DATABASE IF EXISTS `{cfg.mysql.database}`")
    boot.close()

    db = DB(cfg)
    db.init_schema(ROOT / "schema.sql")
    check("建库建表", True, cfg.mysql.database)

    fake = sdk_mod.FakeSdk(total=12)
    puller = Puller(cfg, db, fake)

    n1 = puller.run_until_idle()
    check("首次拉取 12 条", n1 == 12, f"got {n1}")
    check("游标推进到 12", db.get_last_seq() == 12, f"got {db.get_last_seq()}")

    n2 = puller.run_until_idle()
    check("重复拉取幂等（0 条）", n2 == 0, f"got {n2}")

    st = db.stats()
    check("消息总数 12", st["messages"]["n"] == 12, str(st["messages"]))
    check("媒体入队 5 条", sum(st["media"].values()) == 5, str(st["media"]))

    store = storage_mod.build(cfg)
    cfg.media.workers = 4
    w = media_mod.MediaWorker(cfg, db, store, lambda: fake)
    res = w.drain()
    check("媒体下载成功 >= 2", res["done"] >= 2, str(res))
    check("过期媒体被识别", res["expired"] >= 1, str(res))

    with db.tx() as c:
        c.execute("SELECT COUNT(*) n FROM media_object WHERE status='done' AND storage_key IS NOT NULL")
        done = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) n FROM chat_message_to")
        tos = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) n FROM chat_room")
        rooms = c.fetchone()["n"]
        c.execute("SELECT raw FROM chat_message WHERE msg_type='text' LIMIT 1")
        raw = c.fetchone()["raw"]
    check("done 行都有 storage_key", done >= 2, f"got {done}")
    check("收件人表有数据", tos == 12, f"got {tos}")
    check("会话表 2 个群", rooms == 2, f"got {rooms}")
    check("raw 保留完整 JSON", '"msgtype"' in raw, raw[:40])

    hits = db.search("报价单")
    check("中文全文检索命中", len(hits) >= 1, f"got {len(hits)} 条")

    # 落盘文件真实存在
    files = [p for p in cfg.local_root().rglob("*") if p.is_file()]
    check("媒体文件确实落盘", len(files) >= 2, f"{len(files)} 个文件")

    db.close()
    print()
    if FAILS:
        print("失败项：", FAILS)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
