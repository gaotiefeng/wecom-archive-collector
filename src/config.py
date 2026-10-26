"""配置加载。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class WecomCfg:
    corpid: str = ""
    secret: str = ""
    private_key_path: str = ""
    private_key_paths: dict = field(default_factory=dict)
    sdk_lib_path: str = ""
    proxy: str = ""
    proxy_password: str = ""
    timeout_seconds: int = 30
    batch_limit: int = 200


@dataclass
class MysqlCfg:
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "wecom_chatdata"
    charset: str = "utf8mb4"
    max_allowed_packet: int = 64 * 1024 * 1024


@dataclass
class PullCfg:
    poll_interval_seconds: int = 5
    max_batches_per_round: int = 50


@dataclass
class MediaCfg:
    enabled: bool = True
    workers: int = 4
    ttl_hours: int = 0  # 0: do not discard media based on an assumed TTL
    max_attempts: int = 8


@dataclass
class StorageCfg:
    type: str = "local"
    local_root: str = "./var/files"
    endpoint: str = ""
    bucket: str = ""
    access_key: str = ""
    secret_key: str = ""
    region: str = ""


@dataclass
class Config:
    mode: str = "real"
    wecom: WecomCfg = field(default_factory=WecomCfg)
    mysql: MysqlCfg = field(default_factory=MysqlCfg)
    pull: PullCfg = field(default_factory=PullCfg)
    media: MediaCfg = field(default_factory=MediaCfg)
    storage: StorageCfg = field(default_factory=StorageCfg)
    timezone_offset_hours: int = 8

    def private_key(self) -> str:
        p = Path(self.wecom.private_key_path)
        if not p.is_absolute():
            p = ROOT / p
        return p.read_text(encoding="utf-8").strip()

    def local_root(self) -> Path:
        p = Path(self.storage.local_root)
        if not p.is_absolute():
            p = ROOT / p
        return p


def _fill(obj: Any, data: dict) -> None:
    for k, v in (data or {}).items():
        if hasattr(obj, k):
            cur = getattr(obj, k)
            if hasattr(cur, "__dataclass_fields__") and isinstance(v, dict):
                _fill(cur, v)
            else:
                setattr(obj, k, v)


def load(path: str | os.PathLike | None = None) -> Config:
    cfg = Config()
    p = Path(path or os.environ.get("CHATDATA_CONFIG") or ROOT / "config.yaml")
    if p.exists():
        _fill(cfg, yaml.safe_load(p.read_text(encoding="utf-8")) or {})
    else:
        raise FileNotFoundError(
            f"找不到配置文件 {p}，先 `cp config.example.yaml config.yaml` 再填。"
        )
    # 环境变量兜底（方便容器里注入密钥）
    cfg.wecom.corpid = os.environ.get("WECOM_CORPID", cfg.wecom.corpid)
    cfg.wecom.secret = os.environ.get("WECOM_CHATDATA_SECRET", cfg.wecom.secret)
    cfg.mysql.password = os.environ.get("MYSQL_PASSWORD", cfg.mysql.password)
    import re
    if cfg.mode not in ("real", "fake"):
        raise ValueError("mode must be real or fake")
    if not re.fullmatch(r"[A-Za-z0-9_]+", cfg.mysql.database):
        raise ValueError("mysql.database must contain only letters, digits, underscores")
    if cfg.storage.type not in ("local", "s3"):
        raise ValueError("storage.type must be local or s3")
    if not 1 <= cfg.wecom.batch_limit <= 1000:
        raise ValueError("batch_limit must be between 1 and 1000")
    if cfg.media.workers < 1 or cfg.media.max_attempts < 1:
        raise ValueError("workers and max_attempts must be positive")
    if cfg.pull.poll_interval_seconds <= 0 or cfg.pull.max_batches_per_round < 1:
        raise ValueError("pull intervals and batch count must be positive")
    return cfg
