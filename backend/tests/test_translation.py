"""翻译流水线测试 (术语强调在 system prompt, 无术语库/TM)。"""
from app.translation.pipeline import TranslationPipeline


def test_translate_segment_returns_translation():
    """任意句都走 LLM, 返回非空译文。"""
    p = TranslationPipeline()
    r = p.translate_segment("Kazatomprom expects to maintain production at 22,500 tU.")
    assert r.target, "译文不应为空"


def test_new_segment_goes_through_llm():
    """新句走 LLM (术语强调在 system prompt)。"""
    p = TranslationPipeline()
    r = p.translate_segment("Cameco spot price and U3O8 supply gap updated.")
    assert r.target, "译文不应为空"


def test_empty_segment():
    p = TranslationPipeline()
    r = p.translate_segment("")
    assert r.target == ""


def test_translate_document():
    """整篇翻译: 每段都产译文。"""
    p = TranslationPipeline()
    segments = [
        "Kazatomprom expects to maintain production at 22,500 tU.",
        "Spot prices rose 12% QoQ on supply concerns.",
        "Cameco U3O8 production restart.",
    ]
    results = p.translate_document(segments)
    assert len(results) == 3
    # 每段都应有译文
    assert all(r.target for r in results)


# ── translate_page_batch (单页批量合并) ──

def _mixed_page_blocks() -> list[dict]:
    """构造一页混合 block: 短块/数字/正文/表格/空块。"""
    return [
        {"block_id": "t-1-1", "text": "$83.35", "block_type": "text"},                       # 短块-跳过
        {"block_id": "t-1-2", "text": "U3O8", "block_type": "text"},                          # 短块-跳过
        {"block_id": "t-1-3", "text": "Spot prices rose 12% on supply tightness.", "block_type": "text"},
        {"block_id": "tbl-1-1", "text": "", "block_type": "table",                             # 表格-跳过
         "headers": ["Q"], "rows": [["1"]]},
        {"block_id": "t-1-4", "text": "", "block_type": "text"},                              # 空块-跳过
        {"block_id": "t-1-5", "text": "Cameco announced restart of U3O8 production.", "block_type": "text"},
    ]


def test_translate_page_batch_translates_tables():
    """表格 block 走批量翻译 (translated_table 填充), 正文 block 被翻译。

    旧版批量路径跳过表格 (target 空) → 下载 PDF 里表格全英文, 与前台单页路径
    行为不一致 (共享缓存, 谁先跑谁生效)。现已统一: 两条路径都翻译表格。
    """
    p = TranslationPipeline()
    blocks = _mixed_page_blocks()
    out = p.translate_page_batch(1, blocks)
    assert len(out) == len(blocks), "返回必须与输入等长"

    tbl = next(r for r in out if r.block_id == "tbl-1-1")
    assert tbl.block_type == "table"
    assert tbl.translated_table, "表格应产出 translated_table"
    assert tbl.translated_table["headers"][0], "表头应有译文"
    assert tbl.translated_table["rows"][0][0] == "1", "数字格原样保留"

    # 正文被翻译了 (非空译文)
    t3 = next(r for r in out if r.block_id == "t-1-3")
    assert t3.target, "正文块译文不应为空"


def test_translate_page_batch_preserves_block_ids():
    """每条译文继承原 block_id + page (这是 bbox 渲染定位的关键)。"""
    p = TranslationPipeline()
    out = p.translate_page_batch(1, _mixed_page_blocks())
    for r in out:
        assert r.page == 1, f"page 未继承: {r.block_id} -> {r.page}"
    # block_id 与输入一一对应
    ids = [r.block_id for r in out]
    assert ids == ["t-1-1", "t-1-2", "t-1-3", "tbl-1-1", "t-1-4", "t-1-5"]


def test_short_block_kept_verbatim():
    """短块/纯数字 (<8字 或 纯数字+单位) 保留原文, 不调 LLM。"""
    p = TranslationPipeline()
    out = p.translate_page_batch(1, _mixed_page_blocks())
    for r in out:
        if r.block_id == "t-1-1":
            assert r.target == "$83.35", f"短价格未保留原样: {r.target}"
        elif r.block_id == "t-1-2":
            assert r.target == "U3O8", f"化学品代号未保留原样: {r.target}"


def test_translate_page_batch_fallback_on_missing(monkeypatch):
    """模型漏译时 (#编号缺失), 自动对该条单条重译兜底。

    用 mock 让 translate_batch 返回少一条结果, 验证不会越界且最终每条都有译文。
    """
    p = TranslationPipeline()

    # 模拟 GLM 漏译: 第 2 条译文返回空
    original_batch = p.llm.translate_batch
    call_count = {"batch": 0, "single": 0}

    def mock_batch(sources, glossary_per, few_shot=None, src_lang='英文', tgt_lang='中文'):
        call_count["batch"] += 1
        # 故意少返回一条 (模拟漏译)
        return [f"译文{i}" for i in range(len(sources) - 1)] + [None]

    def mock_single(source, glossary_hits, few_shot=None, src_lang='英文', tgt_lang='中文'):
        call_count["single"] += 1
        return f"[单条兜底] {source}"

    p.llm.translate_batch = mock_batch
    p.llm.translate = mock_single

    blocks = [
        {"block_id": "t-1-1", "text": "First sentence here.", "block_type": "text"},
        {"block_id": "t-1-2", "text": "Second sentence here.", "block_type": "text"},
    ]
    out = p.translate_page_batch(1, blocks)
    assert len(out) == 2
    # 第 2 条靠兜底也有译文了 (不为空)
    assert out[1].target, "漏译的条目兜底后应有译文"
    assert "单条兜底" in out[1].target, f"应走单条重译: {out[1].target}"
    assert call_count["batch"] == 1, "应调一次批量"
    assert call_count["single"] == 1, "漏译那条应走一次单条兜底"


# ── 格式修复后的新行为 ──

def test_short_heading_gets_translated():
    """短英文标题 (Summary/Impact) 必须翻译, 不能因 <8 字符留英文。

    旧版 len<8 一刀切跳过 → 版面中英混杂。
    """
    p = TranslationPipeline()
    blocks = [
        {"block_id": "t-1-1", "text": "Summary", "block_type": "text"},
        {"block_id": "t-1-2", "text": "Impact", "block_type": "text"},
    ]
    out = p.translate_page_batch(1, blocks)
    for r in out:
        assert r.target and r.target != r.source, \
            f"短标题应被翻译: {r.source} -> {r.target}"


def test_keep_as_is_patterns():
    """数字/单位/代号保留原样, 不进 LLM。"""
    from app.translation.pipeline import keep_as_is
    kept = ["78.50", "$83.35", "12.3%", "78.50 USD/lb", "U3O8", "UF6", "SWU",
            "tU", "3Mlb", "2026", "2026-2030", "Q1 2026", "Q1", "1,200"]
    for t in kept:
        assert keep_as_is(t), f"应保留原样: {t}"
    translated = ["Summary", "Impact", "Spot prices rose 12% on supply tightness.",
                  "Jan", "Cameco"]
    for t in translated:
        assert not keep_as_is(t), f"不应保留原样: {t}"


def test_column_reading_order():
    """双栏阅读顺序: full 块分隔, 段内先左栏后右栏 (仅影响送翻顺序)。"""
    from app.translation.pipeline import _column_reading_order
    blocks = [
        {"block_id": "a", "column": "full"},     # 0 通栏标题
        {"block_id": "b", "column": "left"},     # 1
        {"block_id": "c", "column": "right"},    # 2
        {"block_id": "d", "column": "left"},     # 3
        {"block_id": "e", "column": "right"},    # 4
        {"block_id": "f", "column": "full"},     # 5 通栏分隔
        {"block_id": "g", "column": "left"},     # 6
    ]
    idxs = [1, 2, 3, 4, 6]
    order = _column_reading_order(blocks, idxs)
    assert order == [1, 3, 2, 4, 6], f"阅读顺序错误: {order}"


def test_translate_page_batch_result_order_stable():
    """送翻顺序重排后, 返回结果仍按原始 block 顺序一一对应 (bbox 渲染定位依赖)。"""
    p = TranslationPipeline()
    blocks = [
        {"block_id": "t-1-1", "text": "Left column first para.", "column": "left"},
        {"block_id": "t-1-2", "text": "Right column first para.", "column": "right"},
        {"block_id": "t-1-3", "text": "Left column second para.", "column": "left"},
    ]
    out = p.translate_page_batch(1, blocks)
    ids = [r.block_id for r in out]
    assert ids == ["t-1-1", "t-1-2", "t-1-3"]
    assert all(r.page == 1 for r in out)


def test_in_image_block_not_translated():
    """图表内部标签 (in_image) 不翻译, 原样保留 —— 图片整体不译。"""
    p = TranslationPipeline()
    blocks = [
        {"block_id": "t-1-1", "text": "Spot Price (US$/lb U3O8) Trend",
         "block_type": "text", "in_image": True},
        {"block_id": "t-1-2", "text": "Body paragraph outside charts.",
         "block_type": "text"},
    ]
    out = p.translate_page_batch(1, blocks)
    img_blk = next(r for r in out if r.block_id == "t-1-1")
    body = next(r for r in out if r.block_id == "t-1-2")
    assert img_blk.target == img_blk.source, "图内标签必须保留原样"
    assert body.target and body.target != body.source, "图外正文正常翻译"


# ── 字号统一 + 标题检测 (渲染层) ──


def test_bold_render_extraction_clean():
    """描边法加粗: 提取文本不含双写字符 (旧双写法会出 '天天' 污染)。"""
    from app.models import TranslationResult
    blocks = [
        {"block_id": "t-1-1", "block_type": "text", "text": "Big Heading",
         "bbox": (50, 30, 400, 70), "font_size": 16.0, "bold": True},
    ]
    tm = {"t-1-1": TranslationResult(source="x", target="重要市场结论标题")}
    import fitz
    from app.translation.bilingual_pdf import render_page_bilingual
    d = fitz.open(); page = d.new_page(width=842, height=595)
    render_page_bilingual(page, blocks, tm)
    txt = page.get_text()
    d.close()
    assert "重要市场结论标题" in txt.replace(" ", "")
    assert "重重要要" not in txt and "天天然然" not in txt


# ── 字号档位 (只调档不扩框版) ──

def test_fontsize_tier_body_dominant_heading_kept():
    """正文起始档=页内主导字号, 标题保留原字号档; 允许长段在原框内平滑降档 ≤1pt。"""
    import fitz
    from app.models import TranslationResult
    from app.translation.bilingual_pdf import render_page_bilingual
    blocks = [
        # 注意: 标题须放在页眉区 (顶部60pt) 之外, 否则按页眉规则被跳过
        {"block_id": "t-1-3", "block_type": "text", "text": "Market Summary",
         "bbox": (50, 66, 400, 96), "font_size": 16.1},
        {"block_id": "t-1-1", "block_type": "text", "text": "First body paragraph.",
         "bbox": (50, 110, 400, 142), "font_size": 10.1},
        {"block_id": "t-1-2", "block_type": "text", "text": "Second body paragraph longer.",
         "bbox": (50, 150, 400, 190), "font_size": 9.7},
        {"block_id": "t-1-4", "block_type": "text", "text": "Third body para.",
         "bbox": (50, 200, 400, 232), "font_size": 10.1},
    ]
    tm = {
        "t-1-1": TranslationResult(source="x", target="第一段正文内容示例。"),
        "t-1-2": TranslationResult(source="x", target="第二段正文内容，长度适中。"),
        "t-1-3": TranslationResult(source="x", target="市场摘要标题"),
        "t-1-4": TranslationResult(source="x", target="第三段正文内容。"),
    }
    d = fitz.open(); page = d.new_page(width=842, height=595)
    render_page_bilingual(page, blocks, tm)
    body_sizes, heading_sizes = [], []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for s in l.get("spans", []):
                t = (s.get("text") or "").strip()
                if not t:
                    continue
                if "标题" in t:
                    heading_sizes.append(s["size"])
                else:
                    body_sizes.append(s["size"])
    d.close()
    assert body_sizes, "应有正文写入"
    # 正文全部落在主导档 10.0 的 [-1.0, +0.6] 区间 (统一观感)
    assert all(9.0 <= s <= 10.6 for s in body_sizes), f"正文档位漂移: {body_sizes}"
    # 最大与最小正文档差 ≤1pt (无 8pt 断崖)
    assert max(body_sizes) - min(body_sizes) <= 1.0, f"正文档位差过大: {body_sizes}"
    assert heading_sizes and min(heading_sizes) >= 12, f"标题档层级丢失: {heading_sizes}"
    assert min(heading_sizes) > max(body_sizes) + 1.5, f"标题与正文层级未拉开: {heading_sizes} vs {body_sizes}"


def test_batch_parse_no_marker_leak():
    """批量解析的分段终点必须是下一标记的开始位置 (不是结束位置)。

    历史 bug: 终点取 end 会把下一个 "[N]" 吞进译文 → 渲染出 "[2]" 杂物
    (用户看到的乱码) 且换行多算一行压低字号。
    """
    from app.llm.glm_client import _parse_batch_segments
    raw = ("[1] 声明\n"
           "[2] TradeTech出版物包括但不限于《The Nuclear Review》。\n"
           "[3] 出版物仅供客户内部使用。")
    p = _parse_batch_segments(raw, 3)
    assert p[1] == ["声明"], f"段1混入标记: {p[1]}"
    assert p[2][0].startswith("TradeTech") and "[3]" not in p[2][0]
    assert "内部使用" in p[3][0]
    # 合并行输出 (不锚定行首)
    p2 = _parse_batch_segments("[1] 甲[2] 乙乙 [3] 丙", 3)
    assert p2[1] == ["甲"] and p2[2] == ["乙乙"]
    # 越界编号不当边界 (其文本并入当前段, 防译文内含 [n] 被误切)
    p3 = _parse_batch_segments("[1] 甲 [9] 无效", 2)
    assert p3 == {1: ["甲 [9] 无效"]}


# ── 字重与颜色 (不加粗 / 首句着色不污染整段) ──

def test_render_font_not_bold():
    """译文渲染字面必须不是粗重字重 (Songti.ttc 0 号面 = Songti SC Black 事故)。"""
    import app.translation.bilingual_pdf as bp
    name = (bp._MEASURE_FONT.name or "") if bp._MEASURE_FONT else ""
    assert not any(k in name for k in ("Bold", "Black", "Heavy", "Semibold")), \
        f"选中了粗重字面: {name}"
    if bp.CJK_FONTFILE:
        assert "Songti.ttc" not in bp.CJK_FONTFILE, "不应直接用 Songti.ttc (0号面是 Black)"


def test_first_sentence_color_does_not_taint_block():
    """段首着色引导句 (少数字符) 不得污染整段颜色/字号 (字符量多数决)。"""
    import fitz, tempfile, os
    from app.parsing.pdf_parser import parse_pdf
    d = fitz.open()
    p = d.new_page(width=842, height=595)
    blue = (0, 0, 1)
    black = (0, 0, 0)
    # 蓝色短引导句 + 黑色长正文 (同一块区域, 解析会合进一个 block)
    p.insert_text((72, 100), "Key insight:", fontsize=13, color=blue)
    p.insert_text((72, 122), "The spot uranium price increased this week overall.", fontsize=10, color=black)
    p.insert_text((72, 136), "Long term prices remained stable across regions.", fontsize=10, color=black)
    p.insert_text((72, 150), "Producers maintained their planned output schedules.", fontsize=10, color=black)
    tmp = os.path.join(tempfile.mkdtemp(), "color_test.pdf")
    d.save(tmp); d.close()
    parsed = parse_pdf(tmp)
    blk = max(parsed.text_blocks, key=lambda b: len(b.text))
    assert blk.color == 0, f"整段被首句颜色污染: color={blk.color}"
    assert abs((blk.font_size or 0) - 10.0) < 0.6, f"字号被引导句带偏: {blk.font_size}"


def test_bold_same_size_title_emphasized():
    """与正文同号的加粗新闻标题 (Arial-BoldMT 型): 应加粗且放大一档展示。"""
    import fitz
    from app.models import TranslationResult
    from app.translation.bilingual_pdf import render_page_bilingual
    blocks = [
        {"block_id": "t-1-1", "block_type": "text", "text": "Market Dynamics",
         "bbox": (50, 60, 400, 80), "font_size": 10.0, "bold": True},
        {"block_id": "t-1-2", "block_type": "text", "text": "Body paragraph text here.",
         "bbox": (50, 90, 400, 122), "font_size": 10.1, "bold": False},
    ]
    tm = {
        "t-1-1": TranslationResult(source="x", target="市场动态"),
        "t-1-2": TranslationResult(source="x", target="正文段落内容示例。"),
    }
    d = fitz.open(); page = d.new_page(width=842, height=595)
    render_page_bilingual(page, blocks, tm)
    title_sizes, body_sizes = [], []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for s in l.get("spans", []):
                t = (s.get("text") or "").strip()
                if not t: continue
                if "动态" in t: title_sizes.append(s["size"])
                else: body_sizes.append(s["size"])
    d.close()
    assert title_sizes and min(title_sizes) >= 11.0, f"标题未放大: {title_sizes}"
    assert body_sizes and abs(body_sizes[0] - 10.0) < 0.6, f"正文档被带偏: {body_sizes}"
    assert min(title_sizes) > max(body_sizes) + 0.5, "标题与正文未拉开"


def test_bold_leadin_paragraph_not_whole_bold():
    """「加粗引导句 + 常规正文」的混合块: 不得整段判粗/放大 (整段加粗事故)。"""
    import fitz, tempfile, os
    from app.parsing.pdf_parser import parse_pdf
    d = fitz.open()
    p = d.new_page(width=842, height=595)
    # 加粗引导句 (在前) + 三行常规正文
    p.insert_text((72, 100), "Kazatomprom announced results:", fontsize=10, color=(0,0,0),
                  fontname="hebo")   # hebo = Helvetica-Bold 内置
    p.insert_text((72, 114), "The company maintained production guidance for the year", fontsize=10, color=(0,0,0))
    p.insert_text((72, 127), "and confirmed its long term contracting strategy with", fontsize=10, color=(0,0,0))
    p.insert_text((72, 140), "utilities across multiple regions and market segments.", fontsize=10, color=(0,0,0))
    tmp = os.path.join(tempfile.mkdtemp(), "leadin.pdf")
    d.save(tmp); d.close()
    parsed = parse_pdf(tmp)
    blk = max(parsed.text_blocks, key=lambda b: len(b.text))
    assert not blk.bold, f"混合块被整块判粗: bold={blk.bold}, text={blk.text[:50]!r}"
    # 纯粗短标题仍应命中
    d2 = fitz.open(); p2 = d2.new_page(width=842, height=595)
    p2.insert_text((72, 100), "Market Dynamics", fontsize=10, fontname="hebo")
    tmp2 = os.path.join(tempfile.mkdtemp(), "title.pdf")
    d2.save(tmp2); d2.close()
    blk2 = parse_pdf(tmp2).text_blocks[0]
    assert blk2.bold, "纯粗标题未被识别"


def test_header_zone_not_translated():
    """顶部页眉区 (块底 ≤ 页高7.5%) 默认不翻译; 紧随其后的正文正常翻译。"""
    p = TranslationPipeline()
    blocks = [
        {"block_id": "h-1", "text": "Nuclear Market Report - Week of Jan 5",
         "block_type": "text", "is_header": True},
        {"block_id": "h-2", "text": "Market Activities", "block_type": "text",
         "bbox": (54, 36, 400, 48)},           # 无标记但几何在页眉区
        {"block_id": "t-1", "text": "Body first line starts here below header.",
         "block_type": "text", "bbox": (54, 60, 400, 92)},
    ]
    out = p.translate_page_batch(1, blocks)
    h1 = next(r for r in out if r.block_id == "h-1")
    t1 = next(r for r in out if r.block_id == "t-1")
    assert h1.target == h1.source, "页眉块必须原样保留"
    assert t1.target and t1.target != t1.source, "页眉下方正文应正常翻译"


def test_dual_font_songti_and_times():
    """中文=宋体、西文/数字=Times New Roman (run 分流); 字号档位不受影响。"""
    import fitz
    from app.models import TranslationResult
    from app.translation.bilingual_pdf import render_page_bilingual, \
        CJK_FONTFILE, LATIN_FONTNAME, _MEASURE_FONT, _LATIN_FONT
    # 选型断言
    assert _MEASURE_FONT is not None and "Songti" in (_MEASURE_FONT.name or ""), \
        f"中文字体应为宋体: {_MEASURE_FONT and _MEASURE_FONT.name}"
    assert LATIN_FONTNAME and _LATIN_FONT is not None and \
        "Times" in (_LATIN_FONT.name or ""), "西文字体应为 Times New Roman"
    assert CJK_FONTFILE and CJK_FONTFILE.endswith(".ttf"), "应使用抽取的宋体单字体而非 TTC"
    # 渲染断言: 输出中同时出现两种字体
    blocks = [
        {"block_id": "t-1-1", "block_type": "text",
         "text": "Spot price rose to 78.50 USD/lb this week.",
         "bbox": (50, 80, 420, 116), "font_size": 10.0},
    ]
    tm = {"t-1-1": TranslationResult(
        source="x", target="TradeTech 现货价格本周涨至 78.50 USD/lb。")}
    d = fitz.open(); page = d.new_page(width=842, height=595)
    render_page_bilingual(page, blocks, tm)
    fonts = set()
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for s in l.get("spans", []):
                if (s.get("text") or "").strip():
                    fonts.add(s.get("font", ""))
    d.close()
    joined = " ".join(fonts)
    assert any("Songti" in f for f in fonts), f"中文未用宋体: {fonts}"
    assert any("Times" in f for f in fonts), f"西文未用 Times: {fonts}"


def test_dynamic_line_spacing():
    """动态行距: 空间富余 → 1.5 (撑满段落), 空间紧 → 回落 1.0-1.2。"""
    import fitz
    from app.models import TranslationResult
    from app.translation.bilingual_pdf import render_page_bilingual
    # 宽度设窄让两段文字都换行成多行
    W = 150
    text = "天然铀现货价格本周小幅上涨，市场供需格局保持平稳，生产商维持生产计划。"
    # 情形A: 高块 (100pt) + 译文只需 2-3 行 → 余量大 → 行距应到 1.5 (pitch=15pt@10pt)
    blocks = [
        {"block_id": "t-1-1", "block_type": "text", "text": "Source para.",
         "bbox": (50, 100, 50 + W, 200), "font_size": 10.0},
    ]
    tm = {"t-1-1": TranslationResult(source="x", target=text)}
    d = fitz.open(); page = d.new_page(width=842, height=595)
    render_page_bilingual(page, blocks, tm)
    ys = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            t = "".join(s.get("text","") for s in l.get("spans", [])).strip()
            if t: ys.append(l["bbox"][1])
    d.close()
    ys.sort()
    assert len(ys) >= 2, f"应换行为多行: {ys}"
    pitch = ys[1] - ys[0]
    assert 17.0 <= pitch <= 19.0, f"富余空间应到 1.8 行距 (pitch≈18pt), 实测 {pitch:.1f}"
    # 情形B: 矮块 (26pt) + 译文 3 行 → 紧 → 行距回落 (pitch ≤ 12pt)
    blocks = [
        {"block_id": "t-1-2", "block_type": "text", "text": "Src.",
         "bbox": (50, 300, 50 + W, 326), "font_size": 10.0},
    ]
    tm2 = {"t-1-2": TranslationResult(source="x", target=text)}
    d2 = fitz.open(); page2 = d2.new_page(width=842, height=595)
    render_page_bilingual(page2, blocks, tm2)
    ys2 = []
    for b in page2.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            t = "".join(s.get("text","") for s in l.get("spans", [])).strip()
            if t: ys2.append(l["bbox"][1])
    d2.close()
    ys2.sort()
    if len(ys2) >= 2:
        pitch2 = ys2[1] - ys2[0]
        assert pitch2 <= 13.0, f"紧凑块行距应回落 ≤1.2, 实测 {pitch2:.1f}"


def test_paragraph_merging():
    """段落重建: 行距连续的同段落行合并为一块 (n_lines 累计), 段落间空白阻断。"""
    import fitz, tempfile, os
    from app.parsing.pdf_parser import parse_pdf
    d = fitz.open()
    p = d.new_page(width=842, height=595)
    # 段落1: 3 行, 行距 13pt (行高 ~10pt, 间隙 3pt)
    p.insert_text((72, 100), "The uranium spot price increased this week overall.", fontsize=10)
    p.insert_text((72, 113), "Market fundamentals remained balanced across regions", fontsize=10)
    p.insert_text((72, 126), "and producers maintained their output plans.", fontsize=10)
    # 段落间空白 (下一行 y=150, 间隙 ~24pt)
    p.insert_text((72, 150), "Long term contracts stayed stable in the period.", fontsize=10)
    p.insert_text((72, 163), "Trading activity was moderate across all regions.", fontsize=10)
    tmp = os.path.join(tempfile.mkdtemp(), "para.pdf")
    d.save(tmp); d.close()
    parsed = parse_pdf(tmp)
    body = [b for b in parsed.text_blocks if not b.in_image and not b.is_header]
    assert len(body) == 2, f"应合并成 2 个段落块, 实得 {len(body)}: {[b.text[:20] for b in body]}"
    assert body[0].n_lines == 3 and body[1].n_lines == 2, \
        f"n_lines 应为 3/2: {[b.n_lines for b in body]}"
    assert "increased this week" in body[0].text and "output plans" in body[0].text


def test_slack_paragraph_vertically_centered():
    """空间富余的段落: 行组垂直居中 (首行下沉, 不再顶部堆挤+底部大洞)。"""
    import fitz
    from app.models import TranslationResult
    from app.translation.bilingual_pdf import render_page_bilingual
    W = 150
    text = "天然铀现货价格本周小幅上涨，市场供需格局保持平稳。"  # 2行
    blocks = [{"block_id": "t-1-1", "block_type": "text", "text": "Src.",
               "bbox": (50, 100, 50 + W, 200), "font_size": 10.0, "n_lines": 6}]
    tm = {"t-1-1": TranslationResult(source="x", target=text)}
    d = fitz.open(); page = d.new_page(width=842, height=595)
    render_page_bilingual(page, blocks, tm)
    ys = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            t = "".join(s.get("text","") for s in l.get("spans", [])).strip()
            if t: ys.append(l["bbox"][1])
    d.close()
    ys.sort()
    assert len(ys) >= 2
    # 段落盒 100-200pt, 首行基线原为 ~109.5, 居中后应明显下沉 (>115)
    assert ys[0] > 115, f"首行应垂直居中下沉, 实测 y0={ys[0]:.1f}"
    # 末行仍在段落盒内
    assert ys[-1] < 200, f"末行超出段落盒: {ys[-1]:.1f}"
    # 行距仍为 1.8 上限
    assert 17.0 <= ys[1] - ys[0] <= 19.0, f"行距应保持 1.8, 实测 {ys[1]-ys[0]:.1f}"


def test_prompt_builder_languages_and_glossary_slot():
    """prompt 语言参数化 + 术语库固定插槽。"""
    from app.llm.glm_client import build_translation_system_prompt
    from app.models import GlossaryEntry
    # 默认英→中: 语言注入 + 术语库插槽常驻 (无内置术语表, 术语全靠用户库)
    p = build_translation_system_prompt("英文", "中文", [])
    assert "将英文原文翻译为中文" in p
    assert "现货价格" not in p                  # 不内置行业术语表
    assert "用户术语库（最高优先级" in p          # 插槽标题常驻
    assert "本次无命中条目" in p                 # 空插槽占位
    # 术语命中: 条目进入插槽, 优先级声明在
    hits = [GlossaryEntry(en="term price", zh_standard="术语价")]
    p2 = build_translation_system_prompt("英文", "中文", hits)
    assert "- term price → 术语价" in p2
    assert "本次无命中条目" not in p2
    # 非中文目标: 无 en→zh 内置表, 通用规则+插槽仍在
    p3 = build_translation_system_prompt("日文", "英文", hits, batch=True)
    assert "将日文原文翻译为英文" in p3
    assert "现货价格" not in p3
    assert "- term price → 术语价" in p3
    assert "[编号]" in p3                        # batch 编号协议
    # 公司名/化学式保留原文的规则语言无关常驻 (通用规则, 不内置具体名单)
    for p_ in (p, p2, p3):
        assert "公司名与机构名保留原文" in p_
        assert "Cameco" not in p_


def test_detect_language_and_prompt_autorule():
    """语言检测 (规则版) + prompt 源语言自适应规则。"""
    from app.parsing.pdf_parser import detect_language
    assert detect_language("The uranium spot price rose and the market stayed stable.") == "英文"
    assert detect_language("Der Uranmarkt ist diese Woche gewachsen und bleibt stabil.") == "德文"
    assert detect_language("Le marché de l uranium est resté stable cette semaine.") == "法文"
    assert detect_language("ウラン価格が上昇した。") == "日文"
    from app.llm.glm_client import build_translation_system_prompt
    p = build_translation_system_prompt("英文", "中文", [])
    assert "源语言自适应" in p and "以原文实际语言为准" in p
