"""原子 JSON 写入与成功任务存储。"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any
from uuid import uuid4


class StoreError(RuntimeError):
    pass


def plan_checksum(plan: dict[str, Any]) -> str:
    canonical = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def checkpoint_checksum(checkpoint: dict[str, Any]) -> str:
    content = {key: value for key, value in checkpoint.items() if key != "checksum"}
    return plan_checksum(content)


def sanitize_json(value: Any) -> Any:
    """把无法严格 JSON 序列化的错误输入保留为可诊断的安全表示。"""
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, float) and not math.isfinite(value):
        return {"invalid_number": str(value)}
    if isinstance(value, dict):
        return {str(sanitize_json(key)): sanitize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json(item) for item in value]
    return value


def write_json(path: Path, data: dict[str, Any], ensure_ascii: bool = False) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=ensure_ascii, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise StoreError(str(exc)) from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # 清理失败不能掩盖原始存储异常或已完成的原子替换。
            pass


class TaskStore:
    def __init__(self, state_dir: Path):
        self.tasks_dir = state_dir / "tasks"
        self.checkpoints_dir = state_dir / "checkpoints"

    def _path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    def _checkpoint_path(self, task_id: str) -> Path:
        return self.checkpoints_dir / f"{task_id}.json"

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, UnicodeError, ValueError) as exc:
            raise StoreError(f"无法读取任务记录 {path.stem}: {exc}") from exc
        if not isinstance(record, dict):
            raise StoreError(f"任务记录 {path.stem} 不是 JSON 对象")
        return record

    def get(self, task_id: str) -> dict[str, Any] | None:
        return self._read(self._path(task_id))

    def save(self, record: dict[str, Any]) -> None:
        write_json(self._path(record["task_id"]), record)

    def get_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        return self._read(self._checkpoint_path(task_id))

    def save_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        write_json(self._checkpoint_path(checkpoint["task_id"]), checkpoint)

    def delete_checkpoint(self, task_id: str) -> None:
        try:
            self._checkpoint_path(task_id).unlink(missing_ok=True)
        except OSError as exc:
            raise StoreError(f"无法清理续跑检查点 {task_id}: {exc}") from exc
