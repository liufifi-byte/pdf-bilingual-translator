"""Stub LLM 客户端 — 占位实现, 本地开发用。

返回结构化示例数据, 让全链路 (解析→翻译→概括) 可跑通。
不依赖任何外部模型服务。配置真实模型见 .env.example。
"""
from __future__ import annotations
from .client import LLMClient
from ..models import GlossaryEntry


class StubClient(LLMClient):
    """占位实现。"""

    def translate(self, source: str, glossary_hits: list[GlossaryEntry],
                  few_shot: list[tuple[str, str]] | None = None,
                  src_lang: str = "英文", tgt_lang: str = "中文") -> str:
        # 简单占位: 前缀标注 + 套用术语标准译法
        result = source
        for term in glossary_hits:
            if term.en in result:
                result = result.replace(term.en, term.zh_standard)
        return f"[译文占位] {result}"

    def summarize_document(self, text: str, src_lang: str = "英文") -> str:
        # 简单占位: 让「AI 概括」按钮全链路可跑通 (真实实现见 glm_client)
        return ("[概括占位] 这是一份外文文档的示例总览。\n"
                f"• 源文档语言: {src_lang}, 正文约 {len(text)} 字符\n"
                "• 占位要点: 核心结论 (见原文)\n"
                "• 占位要点: 关键数据 (见原文)")
