"""LLM 客户端可插拔接口。

业务代码只依赖 LLMClient 抽象, 不依赖具体模型实现。
- 开发/本地: StubClient (返回占位数据, 全链路可跑通, 无需 key)
- 生产: GLMClient (智谱云 API 或任何 OpenAI 兼容 endpoint)

切换只需改 backend/.env 的 LLM_BACKEND, 业务代码零改动。
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import GlossaryEntry


def get_llm_client() -> "LLMClient":
    """根据 config.LLM_BACKEND 返回对应实现。

    延迟导入避免循环依赖 (stub_client/glm_client 反向依赖 LLMClient)。
    """
    from ..config import LLM_BACKEND
    if LLM_BACKEND == "stub":
        from .stub_client import StubClient
        return StubClient()
    elif LLM_BACKEND in ("glm", "openai_compatible"):
        try:
            from .glm_client import GLMClient
            return GLMClient()
        except (ImportError, RuntimeError):
            from .stub_client import StubClient
            return StubClient()
    else:
        from .stub_client import StubClient
        return StubClient()


class LLMClient(ABC):
    """模型层抽象接口。"""

    @abstractmethod
    def translate(self, source: str, glossary_hits: list["GlossaryEntry"],
                  few_shot: list[tuple[str, str]] | None = None,
                  src_lang: str = "英文", tgt_lang: str = "中文") -> str:
        """翻译单段文本。

        Args:
            source: 源语言原文
            glossary_hits: 本段命中的术语 (注入 prompt 术语库插槽)
            few_shot: TM 模糊匹配的句对 (作范例)
            src_lang / tgt_lang: 源/目标语言 (用户可选, 默认 英→中)
        Returns:
            目标语言译文
        """

    def translate_batch(self, sources: list[str],
                        glossary_hits_per: list[list["GlossaryEntry"]],
                        few_shot: list[tuple[str, str]] | None = None,
                        src_lang: str = "英文", tgt_lang: str = "中文"
                        ) -> list[str]:
        """批量翻译多段文本 (单页所有文本块合并成 1 次调用)。

        默认实现 = 循环调 translate。具体模型实现 (如 GLMClient) 可覆盖本方法,
        用编号协议把多段拼成 1 次请求, 大幅减少 API 调用次数 (典型 30 次 → 1 次)。

        Args:
            sources: 多段原文
            glossary_hits_per: 每段命中的术语 (与 sources 等长)
            few_shot: TM 模糊匹配的句对 (作范例)
        Returns:
            译文列表 (与 sources 等长, 一一对应)
        """
        return [self.translate(s, gh, few_shot, src_lang, tgt_lang)
                for s, gh in zip(sources, glossary_hits_per)]

    def summarize_document(self, text: str, src_lang: str = "英文") -> str:
        """整份文档 AI 概括 (顶部「AI 概括」按钮)。

        Args:
            text: 去页眉/图表后的正文拼接 (端点层已截断)
            src_lang: 文档语言 (自动检测结果, 供 prompt 说明)
        Returns:
            中文结构化概括 (一句话总览 + 要点条目)
        """
        raise NotImplementedError
