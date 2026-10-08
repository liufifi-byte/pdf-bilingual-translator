"""PDF 解析 — 双栏识别 + 文本/表格抽取。

针对"国际论文式双栏长报告"特点:
- 按页面坐标判断左右栏 (文本块中心 x 坐标 < 页宽中点 → 左栏)
- 按阅读顺序重排 (先左栏自上而下, 再右栏自上而下)
- 表格用 PyMuPDF find_tables() 抽取, 保留结构

解析完 PyMuPDF 后, 调 GLM-OCR 校正 bbox 坐标 + 版面角色,
让双栏页面的 bbox 不再串行, 翻译原位渲染坐标准确。

输出 ParsedDocument (text_blocks + table_blocks)。
"""
from __future__ import annotations
from pathlib import Path
import re

import fitz  # PyMuPDF

from ..models import ParsedDocument, TextBlock, TableBlock


# 不该进 LLM 的内容: 纯数字/价格/单位/日期/代号 (翻译与表格判定共用)。
# 注意短英文标题 (Summary/Impact 等) 不在此列, 必须翻译, 否则版面中英混杂。
_KEEP_AS_IS_RE = re.compile(
    r"""^(?:
        [$€£¥]?[\d,.\-+/±]+\s*[%‰]?$                       # 78.50 / 1,200 / -3.2 / $83.35 / 12.3%
      | [$€£¥]?[\d,.\-+/±]+\s*[A-Za-zμ²³/%]+$              # 78.50 USD/lb / 3Mlb / 2026-2030
      | \d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?               # 01/15 / 01/15/26 数字型日期
      | U3O8|UF6|SWU|tU|kgU|kgSWU|Mlb|Mlbs|lbs?|kg|t       # 行业代号/单位
      | HALEU|LEU|EUP|ISL|ISR|USA?|UK|EU|EDF|KEPCO
      | Q[1-4](?:[\s\-/]?\d{2,4})?                         # Q1 / Q1 2026
    )$""",
    re.VERBOSE,
)


def keep_as_is(text: str) -> bool:
    """数字/单位/代号判定: 不调 LLM, 渲染时原样保留。"""
    return bool(_KEEP_AS_IS_RE.match((text or "").strip()))


# 数值密集判定字符类: 数字 + 价格/百分比/日期/括号等符号
_NUMSYMBOL_RE = re.compile(r"[\d$%.,+\-/()®:;'\"]")


def numeric_dominant(text: str) -> bool:
    """数值主导块判定: 数字/价格符号量 ≥ 字母量 1.1 倍 → 视为"价格指标条"类,
    不翻译不擦写 (原样保留)。判定依据 (2026-08-21 UxC 乱码事故): 首页
    "Ux Price Indicators" 框是多格窄条文本, 解析层把多格并成一块后译文
    塞不进窄格, 逐字换行排成竖列 = 乱码; 且此类块信息全在数字里, 中文
    重写只损排版不增信息。正文句字母占优不会误伤。"""
    t = (text or "").strip()
    if not t:
        return False
    letters = len(re.findall(r"[A-Za-z]", t))
    numsyms = len(_NUMSYMBOL_RE.findall(t))
    if numsyms == 0:
        return False
    return numsyms >= 1.1 * max(letters, 1)


def plausible_table(extracted: list) -> bool:
    """检出表是否像真数据表, 还是图表网格/版权栏的误检。

    find_tables 在图表密集页会把平铺图表格子、页脚条带误检成表;
    误检表一旦进入翻译流会被整块擦除+按假想网格重写, 与同区域文本块
    翻译双写叠印 (格式灾难)。宁缺毋滥: 未过门的表其文本仍按普通文本块
    逐段原位翻译, 数字原样保留, 不会丢内容。

    判据: 格子非空率 ≥ 0.5; 碾压多行/超长格占比 ≤ 0.1 (轴标特征,
    真数据表极少出现); 规模太小(<4格)直接拒。
    """
    cells = [str(c or "").strip() for row in extracted for c in row]
    nonempty = [c for c in cells if c]
    if len(nonempty) < 4:
        return False
    if len(nonempty) / len(cells) < 0.5:
        return False
    mashed = sum(1 for c in nonempty
                 if len(c) > 40 or c.count("\n") >= 2)
    if mashed / len(nonempty) > 0.1:
        return False
    return True


# ── GLM-OCR 增强 ──

_GLM_OCR_URL = "https://open.bigmodel.cn/api/paas/v4/layout_parsing"
_GLM_OCR_ENABLED = False  # 默认关; 翻译流程走 parse_pdf 时自动开关


def _ocr_enhance_bbox(file_path: Path, num_pages: int) -> dict | None:
    """调 GLM-OCR 解析全篇 PDF, 返回 {page_num: [{bbox_2d, label, content}]}。

    用于覆盖 PyMuPDF 的 bbox 坐标(双栏页面坐标更准)。
    失败返回 None(降级走 PyMuPDF 原坐标)。

    bbox_2d 是归一化坐标(0-1), 需乘页面宽高转绝对坐标。

    GLM-OCR 限制: PDF≤100 页/50MB, 超过需分批提交。
    """
    import os, base64, httpx, time, math

    # 优先从 config 读(后端进程里 .env 已加载), 兜底os.getenv
    try:
        from ..config import GLM_API_KEY as _cfg_key
        api_key = _cfg_key or os.getenv("GLM_API_KEY", "")
    except Exception:
        api_key = os.getenv("GLM_API_KEY", "")
    if not api_key:
        print("[pdf_parser] GLM-OCR: 无 API key, 跳过")
        return None
    try:
        from ..llm.glm_client import _zhipu_jwt
        token = _zhipu_jwt(api_key)
    except Exception:
        return None

    # 检查 PDF 页数, 超 100 页分批
    doc = fitz.open(str(file_path))
    total_pages = len(doc)
    doc.close()

    OCR_MAX_PAGES = 5  # 每批 5 页(实测 12 页会超时, 5 页稳定)
    batches = []
    for start in range(0, total_pages, OCR_MAX_PAGES):
        end = min(start + OCR_MAX_PAGES - 1, total_pages - 1)
        batches.append((start, end))

    hdr = {"Content-Type": "application/json",
           "Authorization": "Bearer " + token}
    result: dict[int, list[dict]] = {}
    total_dt = 0

    for batch_idx, (start, end) in enumerate(batches):
        try:
            # 截取这批页成临时 PDF
            src = fitz.open(str(file_path))
            tmp = fitz.open()
            tmp.insert_pdf(src, from_page=start, to_page=end)
            src.close()
            pdf_b64 = base64.b64encode(tmp.tobytes()).decode()
            tmp.close()

            payload = {"model": "glm-ocr",
                       "file": "data:application/pdf;base64," + pdf_b64}
            t0 = time.time()
            print(f"[pdf_parser] GLM-OCR 批次 {batch_idx+1}/{len(batches)} "
                  f"(p{start+1}-{end+1})...")
            resp = httpx.post(_GLM_OCR_URL, headers=hdr, json=payload, timeout=300)
            dt = time.time() - t0
            total_dt += dt
            if resp.status_code != 200:
                print(f"[pdf_parser] GLM-OCR 批次 {batch_idx+1} 失败 "
                      f"{resp.status_code}: {resp.text[:200]}")
                continue
            data = resp.json()
            layout = data.get("layout_details", [])
            for page_idx, elems in enumerate(layout):
                page_num = start + page_idx + 1  # 加回批次偏移
                clean = []
                for e in elems:
                    bbox = e.get("bbox_2d") or []
                    label = e.get("label", "")
                    content = (e.get("content") or "").strip()
                    if len(bbox) == 4 and label in ("text", "table") and content:
                        clean.append({"bbox_2d": bbox, "label": label,
                                      "content": content})
                if clean:
                    result[page_num] = clean
        except Exception as e:
            print(f"[pdf_parser] GLM-OCR 批次 {batch_idx+1} 异常: {e}")

    if result:
        print(f"[pdf_parser] GLM-OCR ✓ {total_dt:.1f}s, "
              f"{len(result)}/{total_pages} 页")
    return result if result else None


def _match_ocr_to_blocks(text_blocks: list[TextBlock],
                         page_dims: dict[int, tuple[float, float]],
                         ocr_data: dict) -> int:
    """把 GLM-OCR 的正确 bbox 覆盖到 PyMuPDF 的 text_blocks。

    匹配策略: 用 PyMuPDF 文字内容的前 30 字在 OCR 同页的 text 元素里做
    子串匹配, 命中则用 OCR 的归一化 bbox × 页面宽高 覆盖 text_block 的 bbox。
    返回覆盖了几个。
    """
    if not ocr_data:
        return 0
    updated = 0
    for tb in text_blocks:
        ocr_elems = ocr_data.get(tb.page, [])
        if not ocr_elems:
            continue
        tb_snippet = tb.text.strip()[:30].lower()
        if len(tb_snippet) < 5:
            continue
        for oe in ocr_elems:
            if oe["label"] != "text":
                continue
            oe_snippet = oe["content"].strip()[:50].lower()
            # 双向子串匹配(PyMuPDF 的块和 OCR 的块粒度可能不一致)
            if (tb_snippet in oe_snippet or oe_snippet[:20] in tb.text.lower()
                    or tb_snippet[:15] in oe_snippet):
                # 命中 → 用 OCR 坐标覆盖
                dims = page_dims.get(tb.page)
                if not dims:
                    break
                pw, ph = dims
                bx0, by0, bx1, by1 = oe["bbox_2d"]
                tb.x0 = bx0 * pw
                tb.y0 = by0 * ph
                tb.x1 = bx1 * pw
                tb.y1 = by1 * ph
                updated += 1
                break
    return updated


def _classify_column(x0: float, x1: float, page_width: float) -> str:
    """根据文本块 x 坐标判断所属栏。"""
    if page_width <= 0:
        return "full"
    center = (x0 + x1) / 2
    mid = page_width / 2
    # 跨中线的块 (宽度 > 页宽 60%) 视为整栏 (标题/页眉)
    if (x1 - x0) > page_width * 0.6:
        return "full"
    if center < mid:
        return "left"
    return "right"


# ── 文档语言检测 (纯规则, 零 LLM 调用) ──
# 文字系统直接判定 (中日韩俄阿等); 拉丁字母语系用功能词频率打分。
# 报告类长文本下停用词区分度足够; 检测不出时默认英文 (90%+ 场景)。

_LATIN_STOPWORDS: dict[str, set[str]] = {
    "英文": {"the", "of", "and", "to", "in", "that", "for", "with", "as",
             "by", "on", "from", "at", "or", "be", "has", "have", "will"},
    "德文": {"der", "die", "und", "das", "ist", "von", "den", "mit",
             "für", "auf", "im", "zu", "bei", "nicht", "als", "auch"},
    "法文": {"le", "la", "les", "de", "des", "et", "est", "du", "dans",
             "pour", "une", "que", "au", "en", "sur", "avec", "plus"},
    "西班牙文": {"el", "los", "las", "y", "que", "en", "por", "con",
                "para", "del", "una", "como", "más", "pero", "sus"},
    "意大利文": {"il", "di", "che", "e", "per", "con", "del", "delle",
                "sono", "nel", "alla", "anche", "più", "come"},
    "葡萄牙文": {"que", "e", "o", "para", "com", "uma", "por", "do",
                "em", "no", "dos", "como", "mais", "seu"},
    "荷兰文": {"van", "het", "een", "en", "is", "dat", "op", "voor",
              "met", "aan", "zijn", "niet", "ook", "bij"},
}


def detect_language(text: str) -> str:
    """检测一段文本的语言, 返回中文语言名 ("英文"/"德文"/"日文"... )。

    规则: 非拉丁文字系统优先 (韩/日/中/俄/阿); 拉丁语系按功能词频率
    最高分判定; 无信号 → 英文 (业务默认, 90%+ 报告为英文)。
    """
    if not text or not text.strip():
        return "英文"
    if any("\uAC00" <= ch <= "\uD7AF" for ch in text):
        return "韩文"       # 谚文
    if any("\u3040" <= ch <= "\u30FF" for ch in text):
        return "日文"       # 假名
    if any("\u0400" <= ch <= "\u04FF" for ch in text):
        return "俄文"       # 西里尔
    if any("\u0600" <= ch <= "\u06FF" for ch in text):
        return "阿拉伯文"
    han = sum(1 for ch in text if "\u4E00" <= ch <= "\u9FFF")
    if han > len(text) * 0.15:
        return "中文"
    # 拉丁语系: 功能词打分
    words = re.findall(r"[a-zà-ÿäöüß-]+", text.lower())
    if not words:
        return "英文"
    scores = {lang: sum(1 for w in words if w in sw)
              for lang, sw in _LATIN_STOPWORDS.items()}
    best = max(scores.items(), key=lambda kv: kv[1])
    if best[1] >= 3:        # 至少 3 个功能词命中才采信
        return best[0]
    return "英文"


def detect_document_language(parsed) -> str:
    """检测整份文档的主导语言 (按块数多数决, 采样前 400 块)。"""
    if not parsed or not parsed.text_blocks:
        return "英文"
    votes: dict[str, int] = {}
    for tb in parsed.text_blocks[:400]:
        t = (tb.text or "").strip()
        if len(t) < 20:
            continue
        lang = detect_language(t[:600])
        votes[lang] = votes.get(lang, 0) + 1
    if not votes:
        return "英文"
    return max(votes.items(), key=lambda kv: kv[1])[0]


def dedupe_page_tables(tables: list) -> list:
    """去掉嵌套/重复检出的表。

    PyMuPDF 对同一可视表格常同时给出「粗外框」和「细内容」两个检出
    (bbox 互相包含), 双双翻译/写入会在同区域叠两份文本。
    规则: 优先保留格子数多的检出, 与已保留表重叠率 >0.6 (按较小面积) 的丢弃。
    返回保持原始检出顺序。
    """
    def _cells_len(t) -> int:
        try:
            return len(t.cells) or 1
        except Exception:
            return 1

    kept: list = []
    for t in sorted(tables, key=_cells_len, reverse=True):
        rt = fitz.Rect(t.bbox)
        dup = False
        for k in kept:
            rk = fitz.Rect(k.bbox)
            inter = rt & rk
            if inter.is_empty:
                continue
            smaller = min(rt.get_area(), rk.get_area())
            if smaller > 0 and inter.get_area() / smaller > 0.6:
                dup = True
                break
        if not dup:
            kept.append(t)
    order = {id(t): i for i, t in enumerate(tables)}
    return sorted(kept, key=lambda t: order[id(t)])


def _detect_chart_zones(page, min_objects: int = 6, min_area: float = 900.0) -> list:
    """聚合页面 drawings 为密集簇, 返回疑似矢量图表区的矩形列表。

    规则: 迭代合并互相重叠/紧邻 (扩 6pt) 的矢量矩形; 成员 ≥ min_objects
    且簇面积 ≥ min_area 视为图表区。网格线/表格线也是矢量, 但表格文本
    翻译由 table 路径处理, 表格簇误标 in_image 会跳过表格翻译 —— 表格
    的线框簇通常矩形极扁 (行线), 用面积和长宽比过滤一部分。"""
    try:
        rects = [fitz.Rect(d["rect"]) for d in page.get_drawings()
                 if not d["rect"].is_empty and d["rect"].get_area() > 1.0]
    except Exception:
        return []
    rects = [r for r in rects if r.width > 2 and r.height > 2]  # 滤掉细线? 保留网格但小
    # 迭代合并 (扩 6pt 视为相邻)
    zones: list[fitz.Rect] = []
    for r in rects:
        grown = fitz.Rect(r.x0 - 6, r.y0 - 6, r.x1 + 6, r.y1 + 6)
        hit = [z for z in zones if not (z & grown).is_empty]
        if hit:
            merged = fitz.Rect(r)
            for z in hit:
                merged |= z
                zones.remove(z)
            zones.append(merged)
        else:
            zones.append(fitz.Rect(r))
    # 二次收敛 (合并后可能新相邻)
    changed = True
    while changed:
        changed = False
        for i in range(len(zones)):
            for j in range(i + 1, len(zones)):
                a, b = zones[i], zones[j]
                ga = fitz.Rect(a.x0 - 6, a.y0 - 6, a.x1 + 6, a.y1 + 6)
                if not (ga & b).is_empty:
                    zones[i] = a | b
                    zones.pop(j)
                    changed = True
                    break
            if changed:
                break
    # 成员计数需要重算 (合并丢失计数) —— 简化: 用面积+包含的原始矩形数
    kept = []
    for z in zones:
        n_members = sum(1 for r in rects if (r & z).get_area() > 0.5 * r.get_area())
        if n_members >= min_objects and z.get_area() >= min_area:
            kept.append(z)
    return kept


def parse_pdf(file_path: str | Path) -> ParsedDocument:
    """解析 PDF, 返回结构化文档 (双栏识别 + 表格抽取)。"""
    file_path = Path(file_path)
    doc = fitz.open(file_path)
    text_blocks: list[TextBlock] = []
    table_blocks: list[TableBlock] = []
    block_counter = 0
    table_counter = 0

    for page_num, page in enumerate(doc, start=1):
        page_width = page.rect.width

        # ── 文本块抽取 (dict 模式: 能拿到字号/字体, 供原位译按原字号渲染) ──
        # dict 结构: {"blocks": [{"bbox":[x0,y0,x1,y1], "lines":[{"spans":[{"text","size","font"...}]}]}]}
        page_blocks = []
        block_seq = 0
        page_dict = page.get_text("dict")
        # 图片区域 (图表): 落在图内的文字是图表内部标签, 整图不译原样保留
        image_rects = [fitz.Rect(b["bbox"]) for b in page_dict.get("blocks", [])
                       if b.get("type", 0) == 1 and b.get("bbox")]
        # 矢量图表区: 位图保护罩不住矢量图 (路径绘制的图表), 其周边密集
        # 小标签被翻译回写会叠成乱码 —— 聚合 drawings 密集簇 (≥6 对象,
        # 面积 ≥900pt²) 视为图表区, 区内文本同 in_image 待遇
        chart_rects = _detect_chart_zones(page)
        for b in page_dict.get("blocks", []):
            if b.get("type", 0) != 0:  # 0 = 文本块
                continue
            x0, y0, x1, y1 = b.get("bbox", [0, 0, 0, 0])
            r_block = fitz.Rect(x0, y0, x1, y1)
            in_image = False
            if image_rects and r_block.get_area() > 0:
                for r_img in image_rects:
                    inter = r_block & r_img
                    if not inter.is_empty and \
                            inter.get_area() >= 0.5 * r_block.get_area():
                        in_image = True
                        break
            # 收集本块所有 span 的文字 + 字号/字体 (取占比最大的字号)
            spans = []
            for line in b.get("lines", []):
                for span in line.get("spans", []):
                    t = (span.get("text") or "").strip()
                    if t:
                        spans.append((t, span.get("size", 0), span.get("font", ""),
                                      span.get("color", 0)))
            if not spans:
                continue
            text = " ".join(s[0] for s in spans)
            # 原文 PDF 常含 \n 换行, blocks 模式天然保留; dict 模式需手动补
            # 这里按行重组, 保持换行结构
            lines_text = []
            for line in b.get("lines", []):
                lt = " ".join(sp.get("text", "") for sp in line.get("spans", [])).strip()
                if lt:
                    lines_text.append(lt)
            text = "\n".join(lines_text) if lines_text else text
            if not text:
                continue
            # 样式取「字符量多数决」而非"字号最大的 span": 段首着色/加大的
            # 引导句不能污染整段 (首句蓝色 → 整段变蓝的事故源)。
            weight_size: dict[float, int] = {}
            weight_color: dict[int, int] = {}
            for t, sz, fn, cl in spans:
                key_s = round(sz * 2) / 2
                weight_size[key_s] = weight_size.get(key_s, 0) + len(t)
                weight_color[cl] = weight_color.get(cl, 0) + len(t)
            font_size = max(weight_size.items(), key=lambda kv: kv[1])[0] or None
            color = max(weight_color.items(), key=lambda kv: kv[1])[0]
            # 矢量图表区: 只保护小字号标签 (<8.5pt)。大字号标题/正文可能
            # 恰好在图表簇附近 (如 'News' 68pt 大标题), 误吞会整段不翻译
            in_chart = False
            if not in_image and chart_rects and r_block.get_area() > 0 and \
                    (font_size or 11) < 8.5:
                for r_z in chart_rects:
                    inter = r_block & r_z
                    if not inter.is_empty and \
                            inter.get_area() >= 0.5 * r_block.get_area():
                        in_chart = True
                        break
            # 字体名: 取多数字号对应的第一个 span (供参考)
            ref = next((s for s in spans if round(s[1] * 2) / 2 == font_size), spans[0])
            font_name = ref[2] or None
            # 粗体也按字符量多数决: 「加粗引导句 + 常规正文」的混合块 (全文档
            # 353 个, 其中 231 个首 span 粗体) 不能整块判粗 —— 否则整段被
            # 当标题加粗放大。真标题是全粗块 (ratio≈1), 阈值 0.6 可分。
            bold_chars = sum(len(t) for t, _sz, fn, _cl in spans
                             if fn and re.search(r"bold|black|heavy", fn, re.I))
            total_chars = sum(len(t) for t, *_ in spans)
            bold = total_chars > 0 and bold_chars / total_chars >= 0.6
            # 斜体同规则多数决 (字体名含 Italic/Oblique): 渲染层用剪切矩阵复原
            italic_chars = sum(len(t) for t, _sz, fn, _cl in spans
                               if fn and re.search(r"italic|oblique", fn, re.I))
            italic = total_chars > 0 and italic_chars / total_chars >= 0.6
            column = _classify_column(x0, x1, page_width)
            # 顶部页眉区 (整块落在页高 7.5% 内): 默认不翻译 —— 页眉条/贴顶
            # 图表标题在窄条里重排必乱; 正文首行 (TT y54 起, 底 y66) 不会误入。
            is_header = y1 <= max(60.0, page.rect.height * 0.075)
            page_blocks.append({
                "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                "text": text, "column": column,
                "seq": block_seq,  # PyMuPDF 原始顺序 (用于稳定排序)
                "font_size": font_size, "font_name": font_name,
                "color": color, "bold": bold, "italic": italic,
                "in_image": in_image,
                "in_chart": in_chart,
                "is_header": is_header,
                "src_lines": max(1, len(lines_text)),  # 块内原始物理行数
            })
            block_seq += 1

        # 重排: 全部块按 y0 升序 + 原始 seq 作为稳定二级键
        # 这样标题 (full 块) 自然插入到对应 y 位置, 不会"先内容后标题"
        # 同时单栏/双栏块按 y 自然排列
        ordered = sorted(page_blocks, key=lambda b: (b["y0"], b["seq"]))

        # ── 段落重建: PDF 解析常把一个段落拆成"一行一块", 每行钉死在原 y
        # 位置 → 行间距永远等于原文行距 (用户观感"全都一样"), 且按行碎片
        # 送翻会切断句子上下文。合并条件: 同栏/同样式/行距连续(间隙 ≤ 行高
        # 35%)/横向显著重叠; 段落间的空白间隙天然阻断合并。──
        def _merge_into_paragraphs(blocks: list[dict]) -> list[dict]:
            merged: list[dict] = []
            for b in blocks:
                if merged:
                    prev = merged[-1]
                    gap = b["y0"] - prev["y1"]
                    # 行高用「平均行高」(块高/块内行数): 直接用块高会把
                    # 多行块间隙阈值放大, 吞掉段落间空白
                    line_h = max(4.0,
                                 (prev["y1"] - prev["y0"]) / max(1, prev.get("src_lines", 1)),
                                 (b["y1"] - b["y0"]) / max(1, b.get("src_lines", 1)))
                    x_overlap = min(prev["x1"], b["x1"]) - max(prev["x0"], b["x0"])
                    min_w = min(prev["x1"] - prev["x0"], b["x1"] - b["x0"])
                    same_para = (
                        prev["column"] == b["column"]
                        and prev.get("in_image") == b.get("in_image")
                        and prev.get("in_chart") == b.get("in_chart")
                        and prev.get("is_header") == b.get("is_header")
                        and prev.get("bold") == b.get("bold")
                        and prev.get("italic") == b.get("italic")
                        and prev.get("color") == b.get("color")
                        and abs((prev.get("font_size") or 11)
                                - (b.get("font_size") or 11)) < 0.6
                        and gap <= line_h * 0.35
                        and x_overlap > 0.55 * max(1.0, min_w)
                    )
                    if same_para:
                        prev["text"] = (prev["text"].rstrip() + " "
                                        + b["text"].lstrip()).strip()
                        prev["x0"] = min(prev["x0"], b["x0"])
                        prev["x1"] = max(prev["x1"], b["x1"])
                        prev["y1"] = b["y1"]
                        prev["n_lines"] += b.get("src_lines", 1)
                        continue
                merged.append(dict(b, n_lines=b.get("src_lines", 1)))
            return merged

        for b in _merge_into_paragraphs(ordered):
            block_counter += 1
            text_blocks.append(TextBlock(
                block_id=f"t-{page_num}-{block_counter}",
                page=page_num,
                column=b["column"],
                text=b["text"],
                x0=b["x0"], y0=b["y0"], x1=b["x1"], y1=b["y1"],
                font_size=b.get("font_size"),
                font_name=b.get("font_name"),
                color=b.get("color"),
                bold=b.get("bold", False),
                italic=b.get("italic", False),
                in_image=b.get("in_image", False),
                in_chart=b.get("in_chart", False),
                is_header=b.get("is_header", False),
                n_lines=b.get("n_lines", 1),
            ))

        # ── 表格抽取 (嵌套重复检出去重: 同一可视表格常被检出粗/细两份) ──
        try:
            tf = page.find_tables()
            raw_tables = tf.tables if hasattr(tf, "tables") else list(tf)
            for tbl in dedupe_page_tables(raw_tables):
                table_counter += 1
                extracted = tbl.extract()
                if not extracted or not plausible_table(extracted):
                    continue
                headers = [str(h or "").strip() for h in extracted[0]]
                rows = [[str(c or "").strip() for c in r] for r in extracted[1:]]
                if not headers or not rows:
                    continue
                table_blocks.append(TableBlock(
                    block_id=f"tbl-{page_num}-{table_counter}",
                    page=page_num,
                    column="full",
                    caption="",
                    headers=headers,
                    rows=rows,
                ))
        except Exception:
            pass

    doc.close()
    return ParsedDocument(
        file_name=file_path.name,
        num_pages=len(fitz.open(file_path)),
        text_blocks=text_blocks,
        table_blocks=table_blocks,
    )


def parse_pdf_with_ocr(file_path: str | Path, enable_ocr: bool = True) -> ParsedDocument:
    """解析 PDF + GLM-OCR 校正 bbox 坐标(用于翻译原位渲染)。

    先用 PyMuPDF 常规解析(文字精度 100%), 再调 GLM-OCR 校正 bbox
    让双栏页面坐标不再串行。OCR 失败自动降级走 PyMuPDF 原坐标。

    Args:
        enable_ocr: True 时调 GLM-OCR 增强坐标; False 退化到 parse_pdf
    """
    parsed = parse_pdf(file_path)

    if not enable_ocr:
        return parsed

    # 调 GLM-OCR
    ocr_data = _ocr_enhance_bbox(Path(file_path), parsed.num_pages)
    if not ocr_data:
        print("[pdf_parser] GLM-OCR 无数据, 降级走 PyMuPDF 原坐标")
        return parsed

    # 收集每页宽高(bbox_2d 是归一化坐标, 要乘回去)
    doc = fitz.open(str(file_path))
    page_dims = {}
    for i in range(len(doc)):
        page_dims[i + 1] = (doc[i].rect.width, doc[i].rect.height)
    doc.close()

    updated = _match_ocr_to_blocks(parsed.text_blocks, page_dims, ocr_data)
    print(f"[pdf_parser] GLM-OCR 校正 {updated}/{len(parsed.text_blocks)} 块 bbox")
    return parsed


# ── 单页级 GLM-OCR 增强(供 get_page_bilingual 按页调) ──
# 缓存: {src_path: {page_num: ocr_elems}} 避免每页重调 OCR
_ocr_page_cache: dict[str, dict[int, list[dict]]] = {}


def _enhance_page_with_ocr(src_path: str, page_num: int,
                           parsed_blocks: list[dict]) -> list[dict]:
    """对单页的 text_blocks 做 GLM-OCR 坐标增强(就地修改 + 返回)。

    策略:
    - 整份 PDF 首次请求时一次调 GLM-OCR 取全部页结果, 缓存住
    - 后续同份 PDF 的其他页直接命中缓存, 0 延迟
    - 用 OCR 的归一化 bbox × 页面宽高 覆盖 parsed_blocks 的 bbox
    """
    import os
    if os.getenv("TRANSLATE_OCR_ENHANCE", "1") == "0":
        return parsed_blocks  # 开关关了, 直接返回

    # 从缓存里拿这份 PDF 的 OCR 结果
    cached = _ocr_page_cache.get(src_path)
    if cached is None:
        # 首次: 调一次 GLM-OCR 解析整份 PDF
        ocr_data = _ocr_enhance_bbox(Path(src_path), 0)
        cached = ocr_data or {}
        _ocr_page_cache[src_path] = cached

    ocr_elems = cached.get(page_num, [])
    if not ocr_elems:
        return parsed_blocks

    # 拿页面宽高(从原 PDF)
    doc = fitz.open(src_path)
    if page_num <= len(doc):
        pw, ph = doc[page_num - 1].rect.width, doc[page_num - 1].rect.height
    else:
        pw, ph = 612, 792  # 默认 US Letter
    doc.close()

    # 遍历 parsed_blocks, 用文字匹配找 OCR 对应元素, 覆盖 bbox
    for b in parsed_blocks:
        if b.get("block_type") != "text":
            continue
        snippet = (b.get("text") or "").strip()[:30].lower()
        if len(snippet) < 5:
            continue
        for oe in ocr_elems:
            if oe.get("label") != "text":
                continue
            oe_text = oe.get("content", "").strip()[:50].lower()
            # 双向子串匹配
            if (snippet in oe_text
                    or oe_text[:20] in (b.get("text") or "").lower()
                    or snippet[:15] in oe_text):
                bx0, by0, bx1, by1 = oe["bbox_2d"]
                b["bbox"] = (bx0 * pw, by0 * ph, bx1 * pw, by1 * ph)
                break

    return parsed_blocks


def get_segments_for_translation(parsed: ParsedDocument) -> list[str]:
    """把解析结果转为待翻译的文本段列表 (正文段落 + 表格的文本内容)。

    表格单独处理: 每个表格作为一个段 (含表头和行, 翻译时保留结构)。
    """
    segments: list[str] = []
    for tb in parsed.text_blocks:
        segments.append(tb.text)
    for tbl in parsed.table_blocks:
        # 表格转为 Markdown 文本翻译, 翻译后由前端按结构渲染
        lines = ["| " + " | ".join(tbl.headers) + " |"]
        lines.append("| " + " | ".join(["---"] * len(tbl.headers)) + " |")
        for row in tbl.rows:
            lines.append("| " + " | ".join(row) + " |")
        segments.append("\n".join(lines))
    return segments


def get_page_blocks(parsed: ParsedDocument) -> dict[int, list[dict]]:
    """把解析结果按页分组, 保留 page/block_id/column/text 结构。

    表格块额外携带 block_type='table' 和 headers/rows, 供 pipeline.translate_table 用。

    Returns:
        {page_num: [{block_id, column, text, block_type, headers?, rows?}, ...]}
    """
    pages: dict[int, list[dict]] = {}
    for tb in parsed.text_blocks:
        pages.setdefault(tb.page, []).append({
            "block_id": tb.block_id, "column": tb.column, "text": tb.text,
            "block_type": "text",
            "bbox": (tb.x0, tb.y0, tb.x1, tb.y1),
            "font_size": tb.font_size,
            "font_name": tb.font_name,
            "color": tb.color,
            "bold": tb.bold,
            "italic": tb.italic,
            "in_image": tb.in_image,
            "in_chart": tb.in_chart,
            "is_header": tb.is_header,
            "n_lines": tb.n_lines,
        })
    for tbl in parsed.table_blocks:
        pages.setdefault(tbl.page, []).append({
            "block_id": tbl.block_id, "column": "full",
            "text": "",
            "block_type": "table",
            "headers": tbl.headers,
            "rows": tbl.rows,
        })
    return {p: pages[p] for p in sorted(pages.keys())}
