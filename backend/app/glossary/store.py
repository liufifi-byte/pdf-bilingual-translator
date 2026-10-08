"""翻译术语库存储 — 内存 list + JSON 文件双写 (MVP 单租户风格).

设计:
- 启动时加载 backend/data/glossary.json (缺失/解析失败 → 空 list, 不报错)
- 每次写操作 (add/update/delete) 同步落盘
- threading.Lock 保护并发写
- 仅暴露最简 2 字段 (en, zh_standard); 复用 models.GlossaryEntry,
  别名字段 (aliases/category/status/source) 静默保留默认值, 不暴露给前端
"""
from __future__ import annotations

import json
import threading

from ..config import GLOSSARY_PATH
from ..models import GlossaryEntry


class GlossaryStore:
    """单例术语库 (模块级实例 = glossary_store)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[GlossaryEntry] = []
        self._load()

    def _load(self) -> None:
        """启动加载 (失败容错, 不抛异常)."""
        try:
            if GLOSSARY_PATH.exists():
                raw = json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))
                self._entries = [
                    GlossaryEntry(**item) for item in raw
                    if isinstance(item, dict) and item.get("en") and item.get("zh_standard")
                ]
        except Exception as e:  # noqa: BLE001
            print(f"[glossary] 加载失败, 用空 list: {e}")
            self._entries = []

    def _persist(self) -> None:
        """同步落盘 (调用方需持锁)."""
        try:
            GLOSSARY_PATH.write_text(
                json.dumps([e.model_dump() for e in self._entries],
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            print(f"[glossary] 保存失败: {e}")

    # ── 读 ─────────────────────────────────────────────
    def list_all(self) -> list[GlossaryEntry]:
        with self._lock:
            return list(self._entries)

    def match(self, text: str) -> list[GlossaryEntry]:
        """大小写不敏感子串匹配; 仅返回 status='approved' 的命中项.

        被 translation/pipeline.py 调用, 把命中项注入 LLM prompt.
        """
        if not text:
            return []
        lower = text.lower()
        with self._lock:
            hits = [
                e for e in self._entries
                if e.status == "approved" and e.en.lower() in lower
            ]
        return hits

    # ── 写 ─────────────────────────────────────────────
    def add(self, en: str, zh_standard: str) -> GlossaryEntry:
        en = (en or "").strip()
        zh = (zh_standard or "").strip()
        if not en or not zh:
            raise ValueError("英文原文和标准译法都必填")
        with self._lock:
            if any(e.en.lower() == en.lower() for e in self._entries):
                raise ValueError(f"术语已存在: {en}")
            entry = GlossaryEntry(en=en, zh_standard=zh, status="approved",
                                  source="user")
            self._entries.append(entry)
            self._persist()
            return entry

    def update(self, en: str, zh_standard: str) -> GlossaryEntry:
        """按 en 定位 (大小写不敏感) 改 zh_standard; 不存在抛 ValueError."""
        en = (en or "").strip()
        zh = (zh_standard or "").strip()
        if not zh:
            raise ValueError("标准译法不能为空")
        with self._lock:
            for e in self._entries:
                if e.en.lower() == en.lower():
                    e.zh_standard = zh
                    self._persist()
                    return e
        raise ValueError(f"术语不存在: {en}")

    def delete(self, en: str) -> bool:
        en = (en or "").strip()
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.en.lower() != en.lower()]
            removed = len(self._entries) < before
            if removed:
                self._persist()
            return removed


glossary_store = GlossaryStore()
