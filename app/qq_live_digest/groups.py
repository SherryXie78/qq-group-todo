"""Persist the user's explicit group allowlist and apply changes to live intake."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unicodedata

from .config import Settings


def _group(group_id: object, name: object) -> dict[str, str]:
    if not isinstance(group_id, str) or not re.fullmatch(r"[1-9][0-9]{4,11}", group_id.strip()):
        raise ValueError("群号须为 5–12 位数字，请填写群聊号，不是个人 QQ 号。")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError("请填写 1–80 个字的群昵称。")
    if any(unicodedata.category(c).startswith("C") for c in name):
        raise ValueError("群昵称不能包含换行或隐藏控制字符。")
    return {"group_id": group_id.strip(), "name": name.strip()}


def load_groups(path: Path, settings: Settings) -> list[dict[str, str]]:
    if not path.exists():
        return [_group(gid, settings.group_aliases.get(gid, gid)) for gid in settings.group_whitelist]
    if path.stat().st_size > 64 * 1024:
        raise ValueError("群管理配置文件过大。")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("groups"), list):
        raise ValueError("群管理配置格式不正确。")
    items = value["groups"]
    if len(items) > 100 or any(not isinstance(item, dict) for item in items):
        raise ValueError("群管理配置格式不正确。")
    groups = [_group(item.get("group_id"), item.get("name")) for item in items]
    if len({item["group_id"] for item in groups}) != len(groups):
        raise ValueError("群管理配置包含重复群号。")
    return groups


def apply_groups(settings: Settings, groups: list[dict[str, str]]) -> None:
    aliases = {item["group_id"]: item["name"] for item in groups}
    # Aliases first: a newly allowed message always has its configured label.
    settings.group_aliases = aliases
    settings.group_whitelist = tuple(aliases)


class GroupManager:
    def __init__(self, path: Path, settings: Settings):
        self.path = path
        self.settings = settings
        self._lock = threading.RLock()
        self._groups = load_groups(path, settings)
        apply_groups(settings, self._groups)

    def snapshot(self) -> dict:
        with self._lock:
            return {"groups": [dict(item) for item in self._groups], "editable": True}

    def update(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("请提交群昵称和群号。")
        action = payload.get("action")
        if action not in ("upsert", "remove"):
            raise ValueError("不支持的群管理操作。")
        entry = _group(payload.get("group_id"), payload.get("name") if action == "upsert" else "移除")
        with self._lock:
            groups = [dict(item) for item in self._groups]
            found = next((i for i, item in enumerate(groups) if item["group_id"] == entry["group_id"]), None)
            if action == "remove":
                if found is not None:
                    groups.pop(found)
            elif found is not None:
                groups[found] = entry
            elif len(groups) >= 100:
                raise ValueError("最多可整理 100 个群，请先移除不需要的群。")
            else:
                groups.append(entry)
            # Commit before changing intake; failed writes leave both states unchanged.
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                                 prefix=".groups-", suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump({"version": 1, "groups": groups}, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            apply_groups(self.settings, groups)
            self._groups = groups
            return {"ok": True, **self.snapshot()}
