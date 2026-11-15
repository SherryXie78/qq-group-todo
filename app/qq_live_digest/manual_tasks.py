"""Local, user-entered tasks; no QQ calls, AI requests, or calendar writes."""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
import uuid
from typing import Any

from .store import Store


def _text(payload: dict, key: str, limit: int, *, multiline: bool = False) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("填写内容过长或格式不正确。")
    if any(unicodedata.category(char).startswith("C") and
           not (multiline and char in "\n\r\t") for char in value):
        raise ValueError("填写内容含有不支持的隐藏字符。")
    return value.strip()


def create_manual_task(store: Store, payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("请填写待办内容。")
    try:
        client_id = str(uuid.UUID(payload.get("client_id", "")))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("提交标识无效，请重新打开添加窗口。") from None
    summary = _text(payload, "summary", 200)
    if not summary:
        raise ValueError("请填写任务名称。")
    group = _text(payload, "group", 80)
    evidence = _text(payload, "note", 8000, multiline=True)
    date = _text(payload, "deadline_date", 10)
    time = _text(payload, "deadline_time", 5)
    deadline = ""
    if time and not date:
        raise ValueError("填写截止时间时，请同时选择截止日期。")
    if date:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or (time and not re.fullmatch(r"\d{2}:\d{2}", time)):
            raise ValueError("截止日期或时间格式不正确。")
        try:
            # The UI explicitly labels date-only input as 23:59, Beijing time.
            moment = dt.datetime.fromisoformat(date + "T" + (time or "23:59"))
        except ValueError:
            raise ValueError("截止日期或时间无效，请重新选择。") from None
        deadline = moment.isoformat(timespec="seconds")
    task_id = store.upsert_task(
        task_key="manual:" + client_id, summary=summary, action=summary,
        category="action", importance=3, deadline=deadline,
        groups=[group] if group else [], evidence=evidence,
        source="manual", status="open", classification_reason="用户手动添加",
        create_only=True,
    )
    return {"ok": True, "id": task_id}
