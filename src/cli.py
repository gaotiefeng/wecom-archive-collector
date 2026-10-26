"""命令行入口。

    python -m src.cli init-db            # 建库建表
    python -m src.cli pull --once        # 拉一轮然后退出
    python -m src.cli pull               # 常驻
    python -m src.cli media              # 单独清媒体队列
    python -m src.cli run                # 拉取 + 媒体，循环
    python -m src.cli stats
    python -m src.cli search 报价单
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import config as config_mod
from . import media as media_mod
from . import sdk as sdk_mod
from . import storage as storage_mod
from .db import DB
from .puller import Puller

ROOT = Path(__file__).resolve().parent.parent


def _setup_log(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="chatdata")
    ap.add_argument("-c", "--config", default=None)
    ap.add_argument("--fake", action="store_true", help="用合成数据跑，不连企微")
    ap.add_argument("--log", default="info")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db")
    p_pull = sub.add_parser("pull")
    p_pull.add_argument("--once", action="store_true")
    sub.add_parser("media")
    sub.add_parser("run")
    sub.add_parser("stats")
    p_s = sub.add_parser("search")
    p_s.add_argument("kw")
    p_s.add_argument("-n", type=int, default=20)

    args = ap.parse_args(argv)
    _setup_log(args.log)

    cfg = config_mod.load(args.config)
    if args.fake:
        cfg.mode = "fake"
    db = DB(cfg)  # 延迟连接：init-db 时库还不存在，不能在此刻就连

    sdk = None
    try:
        if args.cmd == "init-db":
            db.init_schema(ROOT / "schema.sql")
            print("建表完成：", cfg.mysql.database)
            return 0

        if args.cmd == "stats":
            print(json.dumps(db.stats(), ensure_ascii=False, indent=2, default=str))
            return 0

        if args.cmd == "search":
            rows = db.search(args.kw, args.n)
            if not rows:
                print("没有命中")
            for r in rows:
                who = r["room_id"] or r["from_user"]
                print(f"[{r['msg_time']}] {who} ({r['msg_type']}) {r['snippet']}")
            return 0

        sdk = sdk_mod.build(cfg, fake=args.fake)
        store = storage_mod.build(cfg)
        factory = lambda: sdk_mod.build(cfg, fake=args.fake)
        if not db.acquire_collector_lock():
            raise RuntimeError("another collector is already running for this database")

        if args.cmd == "pull":
            p = Puller(cfg, db, sdk)
            if args.once:
                n = p.run_until_idle()
                print(f"本轮落库 {n} 条，游标 -> {db.get_last_seq()}")
            else:
                p.run_forever()
            return 0

        if args.cmd == "media":
            w = media_mod.MediaWorker(cfg, db, store, factory)
            print(w.drain())
            return 0

        if args.cmd == "run":
            p = Puller(cfg, db, sdk)
            w = media_mod.MediaWorker(cfg, db, store, factory)
            while True:
                n = p.run_until_idle()
                w.drain()
                if n == 0:
                    import time
                    time.sleep(cfg.pull.poll_interval_seconds)
            # unreachable
    finally:
        if sdk is not None:
            sdk.close()
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
