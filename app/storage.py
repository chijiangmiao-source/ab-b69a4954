"""冻结审计证据存储。

记录一旦写入永不修改：

* 相同 ``audit_id`` + 字节级规范化后完全相同的载荷 -> 复用同一记录；
* 相同 ``audit_id`` + 不同载荷 -> :class:`ConflictError`，原有证据不变；
* 非法输入在校验阶段被拒绝，不会产生或覆盖任何文件。

实现为目录下每条记录一个 JSON 文件（审计编号已由校验限制为安全字符），
临时文件 + fsync + 原子 rename 落盘；进程内锁保证并发提交的一致性。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from typing import Any, Optional


class ConflictError(Exception):
    def __init__(self, existing_hash: str, incoming_hash: str):
        self.existing_hash = existing_hash
        self.incoming_hash = incoming_hash
        super().__init__(
            f"审计编号已被不同载荷占用 (existing={existing_hash[:12]}, "
            f"incoming={incoming_hash[:12]})"
        )


class RecordNotFound(KeyError):
    pass


def canonical_hash(payload: dict) -> str:
    """对载荷做规范化哈希（整数按精确值序列化，无浮点介入）。"""
    blob = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class FrozenStore:
    def __init__(self, directory: str):
        self._dir = directory
        os.makedirs(self._dir, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, audit_id: str) -> str:
        # audit_id 已通过 ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ 校验，
        # 不存在路径穿越空间；这里再做一次防御性检查。
        if os.sep in audit_id or audit_id in (".", "..") or "/" in audit_id:
            raise ValueError("unsafe audit id")
        return os.path.join(self._dir, audit_id + ".json")

    def get(self, audit_id: str) -> Optional[dict]:
        path = self._path(audit_id)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def submit(
        self,
        audit_id: str,
        payload: dict,
        result: dict,
        created_at: str,
    ) -> tuple[dict, bool]:
        """提交审计。

        返回 ``(record, reused)``；载荷不同时抛 :class:`ConflictError`。
        """
        incoming_hash = canonical_hash(payload)
        with self._lock:
            existing = self.get(audit_id)
            if existing is not None:
                if existing["payload_sha256"] != incoming_hash:
                    raise ConflictError(existing["payload_sha256"], incoming_hash)
                # 完全相同载荷：返回同一条冻结记录。
                return existing, True

            record = {
                "audit_id": audit_id,
                "created_at": created_at,
                "payload_sha256": incoming_hash,
                "payload": payload,
                "result": result,
            }
            self._atomic_write(self._path(audit_id), record)
            return record, False

    def _atomic_write(self, path: str, record: dict) -> None:
        directory = os.path.dirname(path)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, allow_nan=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
