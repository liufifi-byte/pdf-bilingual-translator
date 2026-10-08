"""术语库路由 — 增删改查 (前端 Modal 用).

风格仿 download.py: 路由前缀写在装饰器里, 不用 prefix= 参数。
仅暴露 en + zh_standard 两字段 (与前端 3 列表格对齐)。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..glossary.store import glossary_store

router = APIRouter()


class GlossaryIn(BaseModel):
    """术语库输入 (新增/修改都用这个最小体)."""
    en: str
    zh_standard: str


@router.get("/api/glossary")
def list_glossary():
    """列出全部术语。"""
    items = glossary_store.list_all()
    return [{"en": e.en, "zh_standard": e.zh_standard} for e in items]


@router.post("/api/glossary")
def add_glossary(body: GlossaryIn):
    """新增术语 (重复 en → 409)。"""
    try:
        entry = glossary_store.add(body.en, body.zh_standard)
    except ValueError as e:
        # 重复 / 空字段 → 409
        raise HTTPException(status_code=409, detail=str(e))
    return {"en": entry.en, "zh_standard": entry.zh_standard}


@router.put("/api/glossary/{en}")
def update_glossary(en: str, body: GlossaryIn):
    """修改术语 (按 en 大小写不敏感定位)."""
    # 路径 en 与 body.en 应一致, 优先用 body.en 防止漂移
    target_en = body.en or en
    try:
        entry = glossary_store.update(target_en, body.zh_standard)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"en": entry.en, "zh_standard": entry.zh_standard}


@router.delete("/api/glossary/{en}")
def delete_glossary(en: str):
    """删除术语 (按 en 大小写不敏感定位)."""
    removed = glossary_store.delete(en)
    if not removed:
        raise HTTPException(status_code=404, detail=f"术语不存在: {en}")
    return {"ok": True, "en": en}
