"""存储后端：本地目录 / S3 兼容对象存储。"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Iterable

_SAFE = re.compile(r"[^0-9A-Za-z._\u4e00-\u9fff-]+")


def safe_name(name: str | None, fallback: str = "blob") -> str:
    if not name:
        return fallback
    base = os.path.basename(name.replace("\\", "/"))
    base = _SAFE.sub("_", base).strip("._") or fallback
    return base[:120]


def build_key(msg_time, msg_id: str, filename: str) -> str:
    """wecom/2026/10/26/<msgid>/<filename> —— 按日期分层，便于冷热分离。"""
    return (
        f"wecom/{msg_time:%Y/%m/%d}/"
        f"{safe_name(msg_id, 'msg')}/{safe_name(filename)}"
    )


class LocalStorage:
    kind = "local"

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, key: str, chunks: Iterable[bytes]) -> tuple[str, str, int]:
        """返回 (key, md5hex, size)。先写临时文件再原子改名，避免半截文件。"""
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        md5 = hashlib.md5()
        size = 0
        fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".part-")
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in chunks:
                    if not chunk:
                        continue
                    md5.update(chunk)
                    size += len(chunk)
                    f.write(chunk)
            os.replace(tmp, dest)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return key, md5.hexdigest(), size

    def url(self, key: str) -> str:
        return str(self.root / key)


class S3Storage:
    kind = "s3"

    def __init__(self, cfg):
        try:
            import boto3  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("storage.type=s3 需要先 pip install boto3") from e
        import boto3

        self.bucket = cfg.bucket
        self._s3 = boto3.client(
            "s3",
            endpoint_url=cfg.endpoint or None,
            aws_access_key_id=cfg.access_key or None,
            aws_secret_access_key=cfg.secret_key or None,
            region_name=cfg.region or None,
        )

    def save(self, key: str, chunks: Iterable[bytes]) -> tuple[str, str, int]:
        # 大文件走磁盘暂存再上传，避免全量吃内存（企微单文件上限 200MB）
        md5 = hashlib.md5()
        size = 0
        fd, tmp_path = tempfile.mkstemp()
        try:
            with os.fdopen(fd, "wb") as tmp:
                for chunk in chunks:
                    if not chunk:
                        continue
                    md5.update(chunk)
                    size += len(chunk)
                    tmp.write(chunk)
            self._s3.upload_file(tmp_path, self.bucket, key)
        finally:
            os.unlink(tmp_path)
        return key, md5.hexdigest(), size

    def url(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"


def build(cfg):
    if cfg.storage.type == "s3":
        return S3Storage(cfg.storage)
    return LocalStorage(cfg.local_root())
