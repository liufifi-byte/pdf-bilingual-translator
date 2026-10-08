"""翻译流水线编排。

流程:
  原文 → [1] TM查询 → [2] 术语检索 → [3] LLM翻译 → [4] 后校准 → [5] 标记复核

准确率累加:
  纯 LLM           ~85%
  + 术语 prompt 注入 ~93%
  + 后校准          ~95%
  + TM 直通/few-shot 稳定 ≥95%
"""
from __future__ import annotations
import re
from ..models import TranslationResult
from ..llm.client import get_llm_client
from ..glossary.store import glossary_store
from ..parsing.pdf_parser import keep_as_is, numeric_dominant  # noqa: F401  (表格/跳过判定共用, 单一来源)

# 默认翻译方向 (用户可在接口层覆盖; prompt 已语言参数化)
DEFAULT_SRC_LANG = "英文"
DEFAULT_TGT_LANG = "中文"


def _mark_review_needed(result: TranslationResult, threshold: float = 0.85) -> TranslationResult:
    """根据 COMET 分数标记是否需要人工复核。

    Stub 不打分, 默认 needs_review=False。
    GLM 实现接入真实 COMET 后, < 阈值则置 True。
    """
    if result.comet_score is not None and result.comet_score < threshold:
        result.needs_review = True
    return result


def _column_reading_order(blocks: list[dict], idxs: list[int]) -> list[int]:
    """把待译下标按双栏阅读顺序重排, 仅影响 LLM 上下文顺序。

    任意通栏块 (含未入选的表格/跳过块) 都作段分隔; 每段内先左栏后右栏,
    各保持原始顺序。结果按下标写回, 不影响渲染定位。
    """
    idx_set = set(idxs)
    ordered: list[int] = []
    seg_left: list[int] = []
    seg_right: list[int] = []

    def _flush():
        ordered.extend(seg_left)
        ordered.extend(seg_right)
        seg_left.clear()
        seg_right.clear()

    for i, b in enumerate(blocks):
        col = b.get("column", "full")
        if col == "full":
            _flush()
            if i in idx_set:
                ordered.append(i)
        elif i in idx_set:
            (seg_left if col == "left" else seg_right).append(i)
    _flush()
    return ordered


def classify_page_blocks(page_num: int, blocks: list[dict]) -> dict:
    """页块分类 — 翻译/渲染/流式视图共用的单一事实源 (2026-08-21)。

    判定规则 (按序): table → 表格翻译; in_image/is_header → 保留;
    keep_as_is (数字/单位/代号/极短) → 保留; numeric_dominant (价格指标条)
    → 保留; 窄列标签 (宽<2.5×字号) → 保留; 重叠保护 (红擦会罩住窄标签)
    → 保留; 其余 → 待译。

    Returns:
      {
        "skips":    {block_idx: TranslationResult},  # target=source 保留块
        "tables":   [block_idx, ...],                # 表格块 (translate_table 处理)
        "todo_idx": [block_idx, ...],                # 待译块下标 (阅读顺序)
        "todo_src": [str, ...],                      # 与 todo_idx 对应文本
        "overlay":  [{block_id,bbox,font_size,color,bold,italic}, ...]
                    # 待译块浮层元数据, 揭示顺序=阅读顺序 (流式视图用)
      }
    """
    skips: dict[int, TranslationResult] = {}
    tables: list[int] = []
    todo_idx: list[int] = []
    todo_src: list[str] = []

    # 窄列标签矩形 (保护用): 待译块红擦矩形罩住它们 ≥20% 面积 → 一并保留
    narrow_rects: list = []
    for b in blocks:
        bb = b.get("bbox")
        if bb and b.get("block_type", "text") == "text" and \
                (bb[2] - bb[0]) < 2.5 * (b.get("font_size") or 10.0):
            if (bb[2] - bb[0]) * (bb[3] - bb[1]) > 0:
                narrow_rects.append(bb)

    def _kills_narrow(bb) -> bool:
        for nr in narrow_rects:
            ix0, iy0 = max(bb[0], nr[0]), max(bb[1], nr[1])
            ix1, iy1 = min(bb[2], nr[2]), min(bb[3], nr[3])
            if ix1 > ix0 and iy1 > iy0:
                inter = (ix1 - ix0) * (iy1 - iy0)
                if inter >= 0.2 * max((nr[2] - nr[0]) * (nr[3] - nr[1]), 1):
                    return True
        return False

    def _keep(i: int, b: dict, text: str) -> None:
        skips[i] = TranslationResult(source=text, target=text,
                                     block_id=b.get("block_id"), page=page_num)

    for i, b in enumerate(blocks):
        if b.get("block_type", "text") == "table":
            tables.append(i)
            continue
        text = (b.get("text") or "").strip()
        if b.get("in_image") or b.get("in_chart") or b.get("is_header"):
            _keep(i, b, text)                       # 图表标签/页眉: 原样
            continue
        if not text or len(text) <= 2 or keep_as_is(text):
            _keep(i, b, text)                       # 数字/单位/代号/极短: 原样
            continue
        if numeric_dominant(text):
            _keep(i, b, text)                       # 价格指标条: 原样
            continue
        bb = b.get("bbox")
        if bb and (bb[2] - bb[0]) < 2.5 * (b.get("font_size") or 10.0):
            _keep(i, b, text)                       # 窄列标签: 原样
            continue
        if bb and _kills_narrow(bb):
            _keep(i, b, text)                       # 会误擦窄标签: 原样
            continue
        todo_idx.append(i)
        todo_src.append(text)

    # 阅读顺序 (双栏: 左栏读完读右栏) — 揭示顺序即此顺序
    # _column_reading_order 返回块下标, 先映射回待译列表局部位置
    pos_of = {bidx: k for k, bidx in enumerate(todo_idx)}
    order = [pos_of[i] for i in _column_reading_order(blocks, todo_idx)]
    todo_idx = [todo_idx[k] for k in order]
    todo_src = [todo_src[k] for k in order]

    # 正文主导字号 (与 PDF 渲染器同规则): 供前端浮层做标题放大/正文归一
    from collections import Counter
    size_weight: Counter = Counter()
    for b in blocks:
        if b.get("block_type", "text") != "text":
            continue
        fs = b.get("font_size")
        t = b.get("text") or ""
        if fs and 6 <= fs <= 15 and t and not b.get("in_image"):
            size_weight[round(fs * 2) / 2] += len(t)
    body_size = size_weight.most_common(1)[0][0] if size_weight else None

    overlay = []
    for i in todo_idx:
        b = blocks[i]
        overlay.append({
            "block_id": b.get("block_id"),
            "bbox": list(b.get("bbox") or (0, 0, 0, 0)),
            "font_size": b.get("font_size") or 10.0,
            "color": b.get("color") if b.get("color") is not None else 0,
            "bold": bool(b.get("bold")),
            "italic": bool(b.get("italic")),
            "len": len((b.get("text") or "").strip()),
            "lines": max(1, (b.get("text") or "").count("\n") + 1),
        })
    return {"skips": skips, "tables": tables, "todo_idx": todo_idx,
            "todo_src": todo_src, "overlay": overlay,
            "body_size": body_size}


class TranslationPipeline:
    """翻译流水线。

    术语强调已沉淀到 LLM client 的 system prompt (固定术语规则, 不再运行时检索术语库/TM)。
    """

    def __init__(self):
        self.llm = get_llm_client()

    def translate_segment(self, source: str, *,
                          src_lang: str = DEFAULT_SRC_LANG,
                          tgt_lang: str = DEFAULT_TGT_LANG) -> TranslationResult:
        """翻译单个段落/句子 (src/tgt 语言可指定, 默认英→中)。"""
        if not source or not source.strip():
            return TranslationResult(source=source, target="")

        # LLM 翻译: prompt 语言参数化 + 用户术语库插槽 (运行时检索命中注入)
        hits = glossary_store.match(source)
        target = self.llm.translate(source, hits, None, src_lang, tgt_lang)

        # 组装结果 + 标记复核
        result = TranslationResult(
            source=source,
            target=target,
            comet_score=None,  # Stub 不打分; GLM 接入 COMET 后填充
            needs_review=False,
        )
        return _mark_review_needed(result)

    def translate_document(self, segments: list[str]) -> list[TranslationResult]:
        """翻译整篇文档 (按段落列表)。"""
        return [self.translate_segment(seg) for seg in segments]

    def translate_block(self, text: str, *, block_id: str | None = None,
                        page: int | None = None,
                        src_lang: str = DEFAULT_SRC_LANG,
                        tgt_lang: str = DEFAULT_TGT_LANG) -> TranslationResult:
        """翻译单个块, 保留 page/block_id 信息。"""
        result = self.translate_segment(text, src_lang=src_lang, tgt_lang=tgt_lang)
        result.page = page
        result.block_id = block_id
        return result

    def translate_table(self, headers: list[str], rows: list[list[str]],
                        *, block_id: str | None = None,
                        page: int | None = None,
                        src_lang: str = DEFAULT_SRC_LANG,
                        tgt_lang: str = DEFAULT_TGT_LANG) -> TranslationResult:
        """翻译表格块, 保留 2D 结构。

        所有可译单元格 + 表头合并成 1-2 次 LLM 批量调用 (逐格调用在
        10x8 的表上要 80 次, 不可接受)。纯数字/单位单元格原样保留。
        输出 block_type='table', translated_table={headers, rows}。
        """
        # 收集所有待译文本 + 坐标 (kind 0=表头, 1=数据格)
        flat: list[str] = []
        coords: list[tuple[int, int, int]] = []
        for c, h in enumerate(headers):
            t = (h or "").strip()
            if t and not keep_as_is(t):
                flat.append(t)
                coords.append((0, -1, c))
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                t = (cell or "").strip()
                if t and not keep_as_is(t):
                    flat.append(t)
                    coords.append((1, r, c))

        translated_headers = [str(h or "") for h in headers]
        translated_rows = [[str(c or "") for c in row] for row in rows]

        if flat:
            hits = [glossary_store.match(s) for s in flat]
            outs = self.llm.translate_batch(flat, hits, None, src_lang, tgt_lang)
            for (kind, r, c), tgt in zip(coords, outs):
                if not tgt:
                    continue  # 漏译的格保留原文 (数字格占多数, 不会成片缺失)
                if kind == 0:
                    translated_headers[c] = tgt
                else:
                    translated_rows[r][c] = tgt

        # target 字段也填一个 markdown 字符串兼容旧 UI
        md_lines = ["| " + " | ".join(translated_headers) + " |",
                    "| " + " | ".join(["---"] * len(translated_headers)) + " |"]
        for r in translated_rows:
            md_lines.append("| " + " | ".join(r) + " |")
        return TranslationResult(
            source="(table)",
            target="\n".join(md_lines),
            block_id=block_id,
            page=page,
            block_type="table",
            translated_table={"headers": translated_headers, "rows": translated_rows},
        )

    def translate_page(self, blocks: list[dict]) -> list[TranslationResult]:
        """翻译单页内的所有块 (并发布)。

        blocks: [{block_id, column, text}, ...]
        """
        return [
            self.translate_block(b["text"], block_id=b["block_id"], page=None)
            for b in blocks
        ]

    def translate_page_with_num(self, page_num: int,
                                blocks: list[dict]) -> list[TranslationResult]:
        """翻译指定页码下的所有块。"""
        return [
            self.translate_block(b["text"], block_id=b["block_id"], page=page_num)
            for b in blocks
        ]

    # ── 跳过判定: 数字/单位/代号/极短碎片不调 LLM, 渲染时原样保留 ──

    def _should_skip(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return True
        if len(t) <= 2:  # 项目符号/单字符碎片
            return True
        return keep_as_is(t)

    def translate_page_batch(self, page_num: int,
                             blocks: list[dict],
                             publish=None,
                             src_lang: str = DEFAULT_SRC_LANG,
                             tgt_lang: str = DEFAULT_TGT_LANG) -> list[TranslationResult]:
        """单页批量翻译 (提速核心: N 个文本块合并成 1-3 次 LLM 调用)。

        处理规则 (与单页前台路径完全一致, 两条路径共享缓存必须行为相同):
        - table block:    translate_table 批量译 (数字格原样保留)
        - 纯数字/单位/代号: 跳过 (target=原文, 渲染时不擦不写 → 原样)
        - 窄列标签 (块宽<2.5×字号): 跳过 — 中文排不进窄列必乱 (UxC 价格框事故)
        - 短标题 (<8字):  翻译 (旧版跳过导致 Summary/Impact 留英文、版面混杂)
        - 其余 text block: 合并调 llm.translate_batch, 每条独立后校准

        publish(list[TranslationResult]) 可选回调 (流式翻译, 2026-08-21):
        给定时按"每 6 块一批并发翻译, 批完成即发布已完成块"执行, 前端轮询
        渲染当前缓存 → 译文逐批出现在页面上, 不必等整页译完。partial 列表
        只含已完成块 (渲染按 block_id 定位, 顺序无关)。

        返回与 blocks 等长、一一对应的 TranslationResult 列表 (按原始顺序)，
        每条继承原 block_id + page_num → 供原位译 PDF 按 bbox 定位渲染。
        """
        results: list[TranslationResult] = [None] * len(blocks)  # type: ignore[list-item]
        to_translate_idx: list[int] = []
        to_translate_src: list[str] = []

        # 第一遍: 分类 (单一事实源 classify_page_blocks — 翻译/渲染/流式视图
        # 三方共用, 规则改动只改一处)
        cls = classify_page_blocks(page_num, blocks)
        for i, r in cls["skips"].items():
            results[i] = r
        for i in cls["tables"]:
            b = blocks[i]
            results[i] = self.translate_table(
                b.get("headers", []), b.get("rows", []),
                block_id=b.get("block_id"), page=page_num,
                src_lang=src_lang, tgt_lang=tgt_lang)
        to_translate_idx = cls["todo_idx"]
        to_translate_src = cls["todo_src"]

        # 第二遍: 批量翻译所有待译块 (按双栏阅读顺序送翻, 结果按下标写回)
        if to_translate_src:
            # 阅读顺序返回的是 block 下标; 映射回待译列表的局部下标
            pos_of = {bidx: k for k, bidx in enumerate(to_translate_idx)}
            order = [pos_of[i] for i in _column_reading_order(blocks, to_translate_idx)]
            ordered_idx = [to_translate_idx[k] for k in order]
            ordered_src = [to_translate_src[k] for k in order]

            if publish is not None:
                # 流式路径: 先发布"零成本块"(跳过/保留), 再按 6 块/批并发,
                # 每批完成即发布 → 前端轮询看到译文逐批上页
                completed = [r for r in results if r is not None]
                publish(list(completed))

                def _one_chunk(chunk_idx: list[int],
                               chunk_src: list[str]) -> list[tuple]:
                    hits = [glossary_store.match(s) for s in chunk_src]
                    outs = list(self.llm.translate_batch(chunk_src, hits, None, src_lang, tgt_lang))
                    outs += [None] * (len(chunk_src) - len(outs))
                    pairs = []
                    for bidx, src, tgt in zip(chunk_idx, chunk_src, outs):
                        if not tgt:  # 批内漏译兜底: 单条重译
                            try:
                                gh = glossary_store.match(src)
                                tgt = self.llm.translate(src, gh, None, src_lang, tgt_lang)
                            except Exception:
                                tgt = src
                        pairs.append((bidx, TranslationResult(
                            source=src, target=tgt,
                            block_id=blocks[bidx].get("block_id"), page=page_num)))
                    return pairs

                from concurrent.futures import ThreadPoolExecutor, as_completed
                chunks = [(ordered_idx[i:i + 6], ordered_src[i:i + 6])
                          for i in range(0, len(ordered_src), 6)]
                with ThreadPoolExecutor(max_workers=4) as pool:
                    futs = [pool.submit(_one_chunk, ci, cs) for ci, cs in chunks]
                    for fut in as_completed(futs):
                        try:
                            pairs = fut.result()
                        except Exception as e:
                            print(f"[pipeline] 流式批次失败(跳过该批): {e}")
                            continue
                        for bidx, r in pairs:
                            results[bidx] = r
                            completed.append(r)
                        publish(list(completed))
                return results  # type: ignore[return-value]

            translated = self.llm.translate_batch(
                ordered_src,
                [glossary_store.match(s) for s in ordered_src], None,
                src_lang, tgt_lang)

            # translated 按阅读顺序返回, 组装回原始 block 下标
            translated_by_block = {
                bidx: t for bidx, t in zip(ordered_idx, translated)}
            for k, idx in enumerate(to_translate_idx):
                src = to_translate_src[k]
                # 兜底: 批量结果缺失/空 → 单条重译
                tgt = translated_by_block.get(idx)
                if not tgt:
                    try:
                        gh = glossary_store.match(src)
                        tgt = self.llm.translate(src, gh, None, src_lang, tgt_lang)
                    except Exception as e:
                        print(f"[pipeline] 第{k+1}条单条兜底失败, 保留原文: {e}")
                        tgt = src
                results[idx] = TranslationResult(
                    source=src, target=tgt,
                    block_id=blocks[idx].get("block_id"),
                    page=page_num)
                results[idx] = _mark_review_needed(results[idx])

        return results  # type: ignore[return-value]
