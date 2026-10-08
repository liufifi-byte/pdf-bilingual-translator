"""数据模型定义 (Pydantic)。"""
from __future__ import annotations
from typing import Literal, Optional
from datetime import datetime
from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────
# 文档解析
# ─────────────────────────────────────────────────────────────
class TextBlock(BaseModel):
    """正文段落。"""
    block_id: str
    page: int
    column: Literal["left", "right", "full"] = "full"
    text: str
    # 坐标 (PDF 用户空间; PyMuPDF 左上原点, 后端按原样记)
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0
    # 原文字号/字体 (渲染时用相同字号写译文, 保持排版对齐; None 时用默认 11)
    font_size: Optional[float] = None
    font_name: Optional[str] = None
    # 原文颜色 (PyMuPDF sRGB 整数) 与粗体/斜体标记, 原位渲染时复原样式
    color: Optional[int] = None
    bold: bool = False
    italic: bool = False
    # 位于图片区域内 (图表的内部标签): 不翻译不擦写, 图片整体保持原样
    in_image: bool = False
    # 位于页面顶部页眉区: 默认不翻译 (页眉/贴顶图表标题原样保留)
    is_header: bool = False
    # 段落重建后的原文物理行数 (渲染层算原文行距用; 行级块=1)
    n_lines: int = 1
    # 位于矢量图表区内 (密集 drawings 聚成的"图表", 非位图): 不译不动
    in_chart: bool = False


class TableBlock(BaseModel):
    """表格 (保留结构)。"""
    block_id: str
    page: int
    column: Literal["left", "right", "full"] = "full"
    caption: str = ""
    headers: list[str]
    rows: list[list[str]]


class ParsedDocument(BaseModel):
    """解析后的结构化文档。"""
    file_name: str
    num_pages: int
    text_blocks: list[TextBlock]
    table_blocks: list[TableBlock]


# ─────────────────────────────────────────────────────────────
# 翻译
# ─────────────────────────────────────────────────────────────
class GlossaryEntry(BaseModel):
    en: str
    zh_standard: str
    aliases: list[str] = Field(default_factory=list)  # 误翻别名
    category: str = ""
    status: Literal["candidate", "reviewing", "approved", "deprecated"] = "approved"
    source: str = ""


class TranslationResult(BaseModel):
    """单句/单段翻译结果。"""
    source: str
    target: str
    matched_terms: list[GlossaryEntry] = Field(default_factory=list)
    comet_score: Optional[float] = None  # 占位, stub 不打分
    needs_review: bool = False
    page: Optional[int] = None     # 所属页码 (按需翻译必需)
    block_id: Optional[str] = None  # 对应解析块 id (溯源用)
    block_type: Literal["text", "table"] = "text"  # 块类型
    translated_table: Optional[dict] = None  # 表格结构: {headers, rows}


# ─────────────────────────────────────────────────────────────
# 文档 (聚合)
# ─────────────────────────────────────────────────────────────
ReportType = Literal["translation"]

ReportStatus = Literal["pending", "parsing", "translating", "done", "failed"]


class Report(BaseModel):
    """一份上传文档的全生命周期。"""
    model_config = {"extra": "allow"}
    id: str
    file_name: str
    upload_time: datetime
    status: ReportStatus = "pending"
    report_type: ReportType = "translation"
    parsed: Optional[ParsedDocument] = None
    translations: list[TranslationResult] = Field(default_factory=list)
    error: str = ""
