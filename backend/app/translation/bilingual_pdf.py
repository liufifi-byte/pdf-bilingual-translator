"""PyMuPDF 原位译 PDF 生成器（PDFMathTranslate 同款方案的加固版）。

流程:
  1. 打开原 PDF
  2. 逐页 render_page_bilingual:
     a. 计划 redact: 只擦「有译文」的文本块 + 「已翻译」的表格 bbox
     b. 一次性 apply_redactions —— 图片/矢量一律不动 (图片不翻译, 原位保留)
     c. 在原 bbox 位置写译文: 中西文混排宽度算字号, 装不下先降字号再向下扩框,
        绝不静默截断; 复原原字号/颜色/粗体
     d. 表格按 find_tables 行列网格逐格写回
  3. 保存为新 PDF

文件命名: {report_id}_bilingual.pdf
"""
from __future__ import annotations
import hashlib
import os
import threading
from pathlib import Path
import fitz  # PyMuPDF

from ..config import UPLOAD_DIR
from ..parsing.pdf_parser import get_page_blocks, numeric_dominant


def _extract_regular_face(ttc_path: str, cache_path: Path,
                          want_name: str = "Songti SC Regular") -> str | None:
    """从 macOS Songti.ttc 抽出常规字重字面存为单字体文件 (缓存复用)。

    Songti.ttc 的 0 号字面是 Songti SC Black (超粗), fontfile=ttc 只能加载
    0 号面 → 必须用 fontTools 抽 Regular 面。抽取约 0.5s, 只在缓存缺失时执行。
    """
    try:
        from fontTools.ttLib import TTCollection
        coll = TTCollection(ttc_path)
        chosen = None
        for font in coll.fonts:
            name = font["name"].getDebugName(4) or ""
            if name == want_name:
                chosen = font
                break
        if chosen is None:  # 退而求其次: 名称含 Regular 且非粗重的第一个
            for font in coll.fonts:
                name = font["name"].getDebugName(4) or ""
                if "Regular" in name and not any(
                        k in name for k in ("Bold", "Black", "Heavy", "Semibold")):
                    chosen = font
                    break
        if chosen is None:
            return None
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        chosen.save(str(cache_path))
        return str(cache_path)
    except Exception as e:
        print(f"[bilingual_pdf] 字面抽取失败 {ttc_path}: {e}")
        return None


def _pick_fonts() -> tuple:
    """选中文字体 + 西文字体, 返回 ((cjk_name, cjk_file, cjk_Font), (latin_name, latin_file, latin_Font))。

    规范: 中文=宋体 (Songti SC Regular), 西文/数字=Times New Roman。
    - Songti.ttc 0 号面是 Black (超粗) → 用 fontTools 抽 Regular 面缓存
      (data/fonts/SongtiSC-Regular.ttf, 缺失时现场抽取);
    - 任何字面名含 Bold/Black/Heavy/Semibold 的一律跳过 (全文加粗事故源);
    - 西文字体缺失时 Latin=None → 西文也用中文字体渲染 (降级)。
    """
    data_dir = Path(__file__).resolve().parent.parent.parent / "data" / "fonts"
    songti_reg = data_dir / "SongtiSC-Regular.ttf"

    cjk_candidates: list[str] = []
    env_cjk = os.getenv("TRANSLATION_CJK_FONTFILE")
    if env_cjk:
        cjk_candidates.append(env_cjk)
    if songti_reg.exists():
        cjk_candidates.append(str(songti_reg))
    if Path("/System/Library/Fonts/Supplemental/Songti.ttc").exists() and not songti_reg.exists():
        extracted = _extract_regular_face(
            "/System/Library/Fonts/Supplemental/Songti.ttc", songti_reg)
        if extracted:
            cjk_candidates.append(extracted)
    # 宋体不可用时的降级链 (Linux 部署等)
    cjk_candidates += [
        "/usr/share/fonts/truetype/simsun/simsun.ttc",            # Linux 宋体
        "/usr/share/fonts/opentype/noto/NotoSerifCJKsc-Regular.otf",
        "/usr/share/fonts/opentype/source-han-serif/SourceHanSerifSC-Regular.otf",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ]

    bold_words = ("Bold", "Black", "Heavy", "Semibold")
    cjk_pick = (None, None, None)
    for path in cjk_candidates:
        if not path or not Path(path).exists():
            continue
        try:
            font_obj = fitz.Font(fontfile=path)
            if any(k in (font_obj.name or "") for k in bold_words):
                continue
            d = fitz.open()
            p = d.new_page()
            p.insert_textbox(fitz.Rect(0, 0, 400, 60), "中英混排Test OK",
                             fontname="cjkreal", fontfile=path, fontsize=10)
            d.close()
            cjk_pick = ("cjkreal", path, font_obj)
            break
        except Exception:
            continue
    if cjk_pick[0] is None:
        try:
            cjk_pick = ("china-ss", None, fitz.Font("china-ss"))
        except Exception:
            cjk_pick = ("china-ss", None, None)

    latin_pick = (None, None, None)
    for path in [os.getenv("TRANSLATION_LATIN_FONTFILE"),
                 "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"]:
        if not path or not Path(path).exists():
            continue
        try:
            font_obj = fitz.Font(fontfile=path)
            if any(k in (font_obj.name or "") for k in bold_words):
                continue
            latin_pick = ("latinreal", path, font_obj)
            break
        except Exception:
            continue
    return cjk_pick, latin_pick


(CJK_FONTNAME, CJK_FONTFILE, _MEASURE_FONT), \
(LATIN_FONTNAME, LATIN_FONTFILE, _LATIN_FONT) = _pick_fonts()


# ── 字体子集化 (2026-08-21 UxC 译文浏览器整页不可见事故对策) ──
# 全量内嵌 20.8MB 宋体 (TTC 抽面保留了苹果原始 cmap 变体) 被 Chromium OTS
# 拒载 ("cmap: bad glyph id offset") → pdfjs 建不出 FontFace → 我们写的所有
# 中文在浏览器不可见 (MuPDF 本地渲染正常, 所以文件本身不空)。fontTools
# subset 按 webfont 标准管线重建全部表 = OTS 安全, 且单页 PDF 从 14.7MB
# 降到百 KB 级。子集按"本页要写的字符集"缓存复用, 原子落盘并发安全。
_SUBSET_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "fonts" / "subsets"
_FONT_OBJ_LOCK = threading.Lock()
_FONT_OBJ_CACHE: dict[str, fitz.Font] = {}


def _subset_font(src_path: str, chars: str, tag: str) -> str | None:
    """fontTools 子集化: 只留 chars 出现过的字形, 返回子集 ttf 路径; 失败 None。"""
    try:
        from fontTools import subset
    except ImportError:
        return None
    if not src_path or not Path(src_path).exists():
        return None
    needed = sorted(set(chars) | {" "})
    key = hashlib.md5(("".join(needed) + src_path).encode("utf-8")).hexdigest()[:12]
    out = _SUBSET_DIR / f"{tag}_{key}.ttf"
    if out.exists():
        return str(out)
    try:
        _SUBSET_DIR.mkdir(parents=True, exist_ok=True)
        opts = subset.Options()
        opts.name_IDs = ["*"]
        opts.name_languages = ["*"]
        opts.glyph_names = False
        opts.notdef_outline = True
        opts.recommended_glyphs = True
        opts.legacy_kern = True
        font = subset.load_font(src_path, opts)
        ss = subset.Subsetter(options=opts)
        ss.populate(unicodes=[ord(c) for c in needed])
        ss.subset(font)
        tmp = out.with_suffix(".tmp")
        subset.save_font(font, str(tmp), opts)
        tmp.replace(out)   # 原子改名: 并发页渲染只见完整文件
        return str(out)
    except Exception as e:
        print(f"[bilingual_pdf] 子集化失败 {src_path}: {e}")
        return None


def _fitz_font(ff: str | None) -> fitz.Font:
    """按字体文件路径取缓存的 fitz.Font (TextWriter 斜体写入用)。"""
    path = ff or CJK_FONTFILE or "helv"
    with _FONT_OBJ_LOCK:
        f = _FONT_OBJ_CACHE.get(path)
        if f is None:
            try:
                f = fitz.Font(fontfile=path)
            except Exception:
                f = fitz.Font("helv")
            _FONT_OBJ_CACHE[path] = f
        return f


def _int_to_rgb(color: int | None) -> tuple[float, float, float]:
    """PyMuPDF sRGB 整数 → (r,g,b) 0-1 元组 (insert_textbox 的 color 参数)。"""
    if not color:
        return (0, 0, 0)
    return ((color >> 16) & 255) / 255, ((color >> 8) & 255) / 255, (color & 255) / 255


def _run_font(is_latin: bool, latin_ff: str | None = None,
              cjk_ff: str | None = None):
    """取 run 对应的 (fontname, fontfile, 度量Font); 西文字体缺时回落中文。
    传入本页子集路径 (latin_ff/cjk_ff) 时写入用子集, 度量仍走全量字体。"""
    if is_latin and (latin_ff or LATIN_FONTNAME):
        return LATIN_FONTNAME, (latin_ff or LATIN_FONTFILE), _LATIN_FONT
    return CJK_FONTNAME, (cjk_ff or CJK_FONTFILE), _MEASURE_FONT


def _split_runs(text: str) -> list[list]:
    """按中西文切 run: [is_latin, chunk] 列表 (ASCII/拉丁 → Times, 其余 → 宋体)。"""
    runs: list[list] = []
    for ch in text:
        is_latin = ord(ch) < 0x2E80
        if runs and runs[-1][0] == is_latin:
            runs[-1][1] += ch
        else:
            runs.append([is_latin, ch])
    return runs


def _text_width(text: str, size: float) -> float:
    """文本宽度: 中西文各自用所选字体的真实度量分别累加。"""
    w = 0.0
    for is_latin, chunk in _split_runs(text):
        _fn, _ff, mfont = _run_font(is_latin)
        if mfont is not None:
            try:
                w += mfont.text_length(chunk, fontsize=size)
                continue
            except Exception:
                pass
        w += sum((size * 0.55 if ord(c) < 128 else size) for c in chunk)
    return w


# 斜体剪切系数 0.26 ≈ 15° (Arial Italic 实测倾角量级; 顶左原点坐标下
# Matrix(1,0,+0.26,1) 让字形上部右移 — 已像素级实验定号)
_ITALIC_SHEAR = 0.26


def _insert_runs_slanted(page, x: float, y: float, text: str, fs: float,
                         rgb, bold: bool = False,
                         cjk_ff: str | None = None,
                         latin_ff: str | None = None) -> None:
    """斜体写入: 同一行各 run 累积进一个 TextWriter, write_text 用剪切矩阵
    (morph) 落盘。中西文统一伪斜体 (CJK 无真斜体字库, 排版惯例即剪切);
    bold 时叠 render_mode=2 描边 (粗斜体)。"""
    tw = fitz.TextWriter(page.rect)
    cur_x = x
    for is_latin, chunk in _split_runs(text):
        if not chunk:
            continue
        _fn, ff, mfont = _run_font(is_latin, latin_ff, cjk_ff)
        try:
            tw.append(fitz.Point(cur_x, y), chunk, font=_fitz_font(ff), fontsize=fs)
        except Exception as e:
            print(f"[bilingual_pdf] 斜体 run 写入失败: {e}")
        if mfont is not None:
            try:
                cur_x += mfont.text_length(chunk, fontsize=fs)
                continue
            except Exception:
                pass
        cur_x += sum((fs * 0.55 if ord(c) < 128 else fs) for c in chunk)
    kw = {"render_mode": 2} if bold else {}
    try:
        tw.write_text(page, color=rgb,
                      morph=(fitz.Point(x, y),
                             fitz.Matrix(1, 0, _ITALIC_SHEAR, 1, 0, 0)), **kw)
    except Exception as e:
        print(f"[bilingual_pdf] 斜体落盘失败(降级正体): {e}")
        try:
            tw.write_text(page, color=rgb)
        except Exception:
            pass


def _insert_runs(page, x: float, y: float, text: str, fs: float,
                 rgb, bold: bool = False, italic: bool = False,
                 cjk_ff: str | None = None,
                 latin_ff: str | None = None) -> float:
    """在基线 (x, y) 写一行中西文混排文本 (逐 run 切换字体), 返回行尾 x。

    中文 run 用宋体、西文/数字 run 用 Times New Roman; bold 时对每个 run
    叠加描边 (render_mode=2, border_width 为字号百分比); italic 时走
    TextWriter 剪切矩阵 (斜体通路无法逐 run insert_text)。
    """
    if italic:
        _insert_runs_slanted(page, x, y, text, fs, rgb, bold=bold,
                             cjk_ff=cjk_ff, latin_ff=latin_ff)
        return x + _text_width(text, fs)
    for is_latin, chunk in _split_runs(text):
        if not chunk:
            continue
        fn, ff, mfont = _run_font(is_latin, latin_ff, cjk_ff)
        kwargs = {"render_mode": 2, "border_width": 0.055} if bold else {}
        try:
            page.insert_text(fitz.Point(x, y), chunk, fontname=fn, fontfile=ff,
                             fontsize=fs, color=rgb, **kwargs)
        except Exception as e:
            print(f"[bilingual_pdf] run 写入失败: {e}")
        if mfont is not None:
            try:
                x += mfont.text_length(chunk, fontsize=fs)
                continue
            except Exception:
                pass
        x += sum((fs * 0.55 if ord(c) < 128 else fs) for c in chunk)
    return x


def _wrap_line_count(text: str, size: float, box_width: float) -> int:
    """按框宽模拟换行后的行数 (尊重原文显式换行)。"""
    if box_width <= 0:
        return max(1, len(text))
    lines = 0
    for para in text.split("\n"):
        if not para:
            lines += 1
            continue
        lines += max(1, int(_text_width(para, size) / box_width) + 0.999)
    return max(1, lines)


# 系统字体 (宋体等) 的 ascender/descender 会在指定 lineheight 之外额外
# 占用约 0.34em 行高 —— 实测 insert_textbox 需要高度 ≈ n*fs*lh + fs*0.4。
# 低估这个常数会误判"装得下", 实际写入失败后直接掉进 8pt 兜底 = 正文忽大忽小。
_LH_OVERHEAD = 0.4


def _calc_fontsize(text: str, box_width: float, box_height: float,
                   base: float = 11, min_size: float = 8.0) -> float:
    """最优字号: 尽量贴原字号(base), 装不下逐档 0.5pt 降; 降到底仍不够返回 min_size
    (由调用方兜底, 不再无脑缩到看不清)。高度需求含行高开销常数 (见 _LH_OVERHEAD)。"""
    if not text:
        return base if 7 <= base <= 30 else 11
    if base < 7 or base > 30:
        base = 11
    size = float(min(base, 24))
    while size >= min_size:
        need = (_wrap_line_count(text, size, box_width) * size * 1.35
                + size * _LH_OVERHEAD)
        if need <= box_height:
            return size
        size -= 0.5
    return min_size


def _insert_text_no_loss(page, rect: fitz.Rect, text: str, fontsize: float,
                         color, max_bottom: float | None = None,
                         preferred_lh: float = 1.35) -> bool:
    """往 rect 写文本, 保证不静默丢字。

    insert_textbox 装不下时什么都不写 (返回负值) —— 旧版在这里直接放弃,
    造成译文被无声截断。本函数逐级兜底: preferred_lh 行距写 → 原框内平滑
    降字号 → 8pt 向下扩框 (不超过 max_bottom=下方最近块顶边, 密集页防止
    压到下一块; 扩框钳制行为保持不变) → 向上扩 → insert_text 裸写。
    返回是否成功。
    """
    def _try(r: fitz.Rect, fs: float, lh: float) -> bool:
        try:
            rc = page.insert_textbox(
                r, text, fontname=CJK_FONTNAME, fontfile=CJK_FONTFILE,
                fontsize=fs, align=fitz.TEXT_ALIGN_LEFT, lineheight=lh,
                color=color)
            return rc >= 0
        except Exception as e:
            print(f"[bilingual_pdf] 文本写入异常 fs={fs}: {e}")
            return False

    # 1) 首选: 调用方按原文行距选好的 (字号, 行距) 组合
    if _try(rect, fontsize, preferred_lh):
        return True
    # 2) 原框内平滑降字号 (0.5pt/档, 只动字号不动框)
    fs = fontsize - 0.5
    while fs >= 8.0:
        if _try(rect, fs, min(preferred_lh, 1.25)):
            return True
        fs -= 0.5
    if fontsize > 6.0 and _try(rect, 6.0, 1.15):
        return True
    # 3) 固定 8pt 向下扩框 (上限: 页底 与 下方最近块顶边 取小;
    #    下方块与本块本就互叠时不钳制, 否则扩无可扩被逼进裸写更糟)
    page_h = page.rect.y1
    hard_bottom = page_h - 2
    if max_bottom and max_bottom > rect.y1 + 2:
        hard_bottom = min(hard_bottom, max_bottom)
    for grow in (rect.height * 0.5, rect.height, rect.height * 2, page_h):
        ext = fitz.Rect(rect.x0, rect.y0, rect.x1, min(rect.y1 + grow, hard_bottom))
        if ext.height <= rect.height + 0.5:
            continue
        if _try(ext, 8.0, 1.2):
            return True
    # 4) 向上扩 (块贴近页底时)
    for grow in (rect.height, rect.height * 2):
        ext = fitz.Rect(rect.x0, max(rect.y0 - grow, 2), rect.x1, rect.y1)
        if _try(ext, 8.0, 1.2):
            return True
    # 5) 最终兜底: 裸写单行 (无换行限制, 必定写入)
    try:
        page.insert_text(fitz.Point(rect.x0, rect.y0 + 8), text,
                         fontname=CJK_FONTNAME, fontfile=CJK_FONTFILE,
                         fontsize=8, color=color)
        print(f"[bilingual_pdf] 块只能单行裸写(可能溢出): {text[:30]}...")
        return True
    except Exception as e:
        print(f"[bilingual_pdf] 块写入彻底失败: {e}")
        return False


def _wrap_lines(text: str, fs: float, width: float) -> list[str]:
    """按宽度把文本切成行 (真实字体度量; 尊重原文显式换行)。"""
    if width <= 0:
        return [text]
    lines: list[str] = []
    for para in text.split("\n"):
        if not para:
            lines.append("")
            continue
        cur = ""
        for ch in para:
            if cur and _text_width(cur + ch, fs) > width:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        lines.append(cur)
    return lines


# 动态行距边界: 下限 1.0 (最紧, 降字号前最后档), 上限 1.8 (空间富余时的
# 舒展档 —— 对标豆包等重排版阅读器的松行距观感, 中文舒适阅读 1.5-1.8)
_LH_DYN_MIN, _LH_DYN_MAX = 1.0, 1.8


def _draw_block_text(page, text: str, bbox: tuple[float, float, float, float],
                     font_size: float | None = None,
                     color: int | None = None, bold: bool = False,
                     italic: bool = False,
                     max_bottom: float | None = None,
                     min_size: float = 8.0,
                     lh: float = 1.25,
                     cjk_ff: str | None = None,
                     latin_ff: str | None = None) -> None:
    """在原 bbox 位置逐行写译文 (英文已在 render 抹掉), **行距动态自适应**。

    机制 (按译文块空间余量): 在候选字号 fs 下中文换行成 n 行后, 可用基线
    空间 = 末行基线上限 (块底+0.5fs 与下方块顶取小) - 首行基线;
    动态行距 = clamp(可用空间 ÷ (n-1) ÷ fs, 1.0, 1.5):
    - 译文比原文短很多 (余量大) → 顶到 1.5, 行距舒展且末行贴近块底 (段落显得饱满);
    - 余量小 → 自然回落 1.2/1.0; 1.0 仍装不下才降 0.5pt 字号 (字号优先级不变)。
    lh 入参仅作单行块的兜底值。bold=标题: 描边加粗。
    """
    if not text or not text.strip():
        return
    x0, y0, x1, y1 = bbox
    rect_w = max(x1 - x0 - 4, 20)
    page_h = page.rect.y1
    hard_bottom = page_h - 2
    if max_bottom and max_bottom > y1 + 2:
        hard_bottom = min(hard_bottom, max_bottom)
    rgb = _int_to_rgb(color)

    base = float(font_size) if (font_size and font_size > 0) else 11.0
    fs = min(base, 24.0)
    floor = min(min_size, fs)
    lh_dyn = lh
    while fs >= floor:
        lines = _wrap_lines(text, fs, rect_w)
        first_bl = y0 + 1 + fs * 0.85
        limit = min(y1 + fs * 0.5, hard_bottom)
        if len(lines) <= 1:
            lh_dyn = lh          # 单行: 行距不参与排版
            break
        pitch = (limit - first_bl) / (len(lines) - 1)
        lh_dyn = min(_LH_DYN_MAX, max(_LH_DYN_MIN, pitch / fs))
        if first_bl + (len(lines) - 1) * fs * lh_dyn <= limit + 0.01:
            break
        fs -= 0.5                # 连 1.0 行距都装不下 → 降字号
    else:
        fs = floor
        lines = _wrap_lines(text, fs, rect_w)
        first_bl = y0 + 1 + fs * 0.85
        limit = min(y1 + fs * 0.5, hard_bottom)
        if len(lines) > 1:
            pitch = (limit - first_bl) / (len(lines) - 1)
            lh_dyn = min(_LH_DYN_MAX, max(_LH_DYN_MIN, pitch / fs))

    lines = _wrap_lines(text, fs, rect_w)
    first_bl = y0 + 1 + fs * 0.85
    limit = min(y1 + fs * 0.5, hard_bottom)
    # 垂直居中: 剩余空间 (>0.5fs 的可见空隙) 一分为二, 行组在段落盒内
    # 上下均衡 —— 否则松段落是"顶上挤 + 底部大洞", 行距的舒展感被洞淹没
    slack = limit - (first_bl + (len(lines) - 1) * fs * lh_dyn)
    y = first_bl + (slack / 2 if slack > fs * 0.5 else 0.0)
    for ln in lines:
        if y > page_h - 2:
            print(f"[bilingual_pdf] 行超出页底被截: {text[:24]}...")
            break
        _insert_runs(page, x0 + 2, y, ln, fs, rgb, bold=bold, italic=italic,
                     cjk_ff=cjk_ff, latin_ff=latin_ff)
        y += fs * lh_dyn


# ── 表格重绘 ──

def _table_grid(tbl) -> list[list] | None:
    """表格行列网格: 优先 rows[r].cells (合并单元格为 None, 行列天然对齐);
    旧 API 无 rows 时退回 cells 平铺按 r*n_cols+c 猜 (可能错位, 仅兜底)。"""
    try:
        grid = [list(r.cells) for r in tbl.rows]
        if grid:
            return grid
    except Exception:
        pass
    try:
        cells = list(tbl.cells)
        n_cols = len(tbl.header.names) if getattr(tbl, "header", None) else 0
        if cells and not n_cols:
            return None
        return [cells[i:i + n_cols] for i in range(0, len(cells), n_cols)] if n_cols else None
    except Exception:
        return None


def _cell_rect(cell) -> fitz.Rect | None:
    if cell is None:
        return None
    if isinstance(cell, fitz.Rect):
        return cell if not cell.is_empty else None
    if isinstance(cell, (tuple, list)) and len(cell) >= 4:
        r = fitz.Rect(cell[:4])
        return r if not r.is_empty else None
    return None


def _write_cell_line(page, inner: fitz.Rect, y_baseline: float, val: str,
                     fs: float, center: bool,
                     cjk_ff: str | None = None,
                     latin_ff: str | None = None) -> None:
    """写单元格一行: 中西文 run 分流 (数字/单位→Times), center=数字类居中。"""
    w = _text_width(val, fs)
    if center:
        x = inner.x0 + max(0, (inner.width - w) / 2)
    else:
        x = inner.x0
    _insert_runs(page, x, y_baseline, val, fs, (0, 0, 0),
                 cjk_ff=cjk_ff, latin_ff=latin_ff)


def _write_table_cells(page, tbl, translated_table: dict,
                       cjk_ff: str | None = None,
                       latin_ff: str | None = None) -> None:
    """按行列网格逐格写回译文 (run 分流: 中文宋体/数字西文 Times);
    数字/单位居中, 文本左对齐; 装不下先降字号再向下扩 (限同列下一格顶边)。"""
    from ..translation.pipeline import keep_as_is

    headers = translated_table.get("headers") or []
    rows = translated_table.get("rows") or []
    grid = _table_grid(tbl)
    if not grid:
        print("[bilingual_pdf] 表格无行列网格, 跳过重写 (保留原表)")
        return
    page_h = page.rect.y1
    for r, cells_row in enumerate(grid):
        src = headers if r == 0 else (rows[r - 1] if r - 1 < len(rows) else None)
        if src is None:
            continue
        for c, cell in enumerate(cells_row):
            rect = _cell_rect(cell)
            if rect is None or c >= len(src):
                continue
            val = str(src[c] or "").strip()
            if not val:
                continue
            inner = fitz.Rect(rect.x0 + 1.5, rect.y0 + 0.5,
                              max(rect.x1 - 1.5, rect.x0 + 6), max(rect.y1 - 0.5, rect.y0 + 4))
            center = keep_as_is(val)
            fs = _calc_fontsize(val, inner.width, inner.height, base=9, min_size=5.0)
            # 扩框上限 = 同列下一行格子顶边 (不跨行侵入), 无下一行则到页底
            next_top = page_h
            for r2 in range(r + 1, len(grid)):
                if c < len(grid[r2]):
                    below = _cell_rect(grid[r2][c])
                    if below and below.y0 > inner.y1:
                        next_top = min(next_top, below.y0 - 1)
            hard_bottom = min(next_top, page_h - 2)
            while fs >= 4:
                lines = _wrap_lines(val, fs, inner.width - 1)
                last_bl = inner.y0 + fs * 0.85 + (len(lines) - 1) * fs * 1.1
                if last_bl <= min(inner.y1, hard_bottom) and \
                        all(_text_width(l, fs) <= inner.width + 0.5 for l in lines):
                    break
                fs -= 0.5
            else:
                fs = 4.0
            lines = _wrap_lines(val, fs, inner.width - 1)
            y = inner.y0 + fs * 0.85
            for ln in lines:
                if y > page_h - 2:
                    break
                _write_cell_line(page, inner, y, ln, fs, center,
                                 cjk_ff=cjk_ff, latin_ff=latin_ff)
                y += fs * 1.1


# ── 单页渲染 (整本 PDF 与前台单页 PDF 共用, 保证两条出口效果一致) ──

def render_page_bilingual(page, blocks: list[dict], trans_map: dict,
                          cjk_ff: str | None = None,
                          latin_ff: str | None = None) -> None:
    """单页原位渲染。

    只 redact 「将被重写」的内容:
    - 文本块: 有译文且译文≠原文 (keep-as-is 块不擦, 保留原始像素)
    - 表格块: 按页内序数匹配 find_tables 结果, 整表 bbox 擦 (网格线是矢量, 不会被删)
    一次性 apply_redactions, 图片/矢量全部保护 (图片不翻译, 原位保留)。
    cjk_ff/latin_ff: 本页写字用的字体文件 (子集); 缺省时按本页待写文本自动子集化。
    """
    text_jobs: list[tuple] = []          # (target, bbox, font_size, color, bold)
    table_blocks: list[dict] = []
    # 图片区域双保险: 解析层已标 in_image, 这里再按当前页图片 bbox 拦一道
    # (兼容改动前已解析入库的旧报告)。图片整体不译不动。
    img_rects = [fitz.Rect(i["bbox"]) for i in page.get_image_info() if i.get("bbox")]

    def _mostly_in_image(rect: fitz.Rect) -> bool:
        if rect.get_area() <= 0:
            return False
        for r in img_rects:
            inter = rect & r
            if not inter.is_empty and inter.get_area() >= 0.5 * rect.get_area():
                return True
        return False

    # ── 字号档位: 与页内主导字号差 <1.2pt 的正文块 → 用主导字号 (统一大小);
    # 显著更大 (标题) / 更小 (脚注) 的块保留原字号。只影响起始档位,
    # 装不下仍按 _calc_fontsize/写入阶梯平滑降档, 不改任何扩框行为。 ──
    from collections import Counter
    size_weight = Counter()
    for b in blocks:
        if b.get("block_type", "text") != "text":
            continue
        fs = b.get("font_size")
        t = b.get("text") or ""
        if fs and 6 <= fs <= 15 and t and not b.get("in_image"):
            size_weight[round(fs * 2) / 2] += len(t)
    body_size = size_weight.most_common(1)[0][0] if size_weight else None

    # 窄列标签矩形预收集 (保护用): 后面任何待译块的红擦矩形若会罩住这些
    # 竖排标签 → 一并跳过。密集价格框的行块 (如 'NA Spot $64.00 CMC...') x 范围
    # 横跨竖标签列, 整块擦除会把标签字符一起带走 → 半截残字 (2026-08-21)。
    narrow_rects: list[fitz.Rect] = []
    for b in blocks:
        if b.get("block_type", "text") != "text":
            continue
        bb = b.get("bbox")
        if not bb:
            continue
        if (bb[2] - bb[0]) < 2.5 * (b.get("font_size") or 10.0):
            narrow_rects.append(fitz.Rect(*bb))

    def _kills_narrow_label(rect: fitz.Rect) -> bool:
        for nr in narrow_rects:
            inter = rect & nr
            if not inter.is_empty and inter.get_area() >= 0.2 * max(nr.get_area(), 1):
                return True
        return False

    for b in blocks:
        if b.get("block_type", "text") == "table":
            table_blocks.append(b)
            continue
        bbox = b.get("bbox")
        text = (b.get("text") or "").strip()
        if not (bbox and text):
            continue
        if b.get("in_image") or b.get("in_chart") or _mostly_in_image(fitz.Rect(*bbox)):
            # 图表内部标签: 图片整体不译不动 (双保险: 解析层标记 + 当前页图片 bbox)
            continue
        if b.get("is_header") or bbox[3] <= max(60.0, page.rect.height * 0.075):
            # 顶部页眉区: 不译不动 (双保险: 解析层标记 + 当前页几何)
            continue
        if numeric_dominant(text):
            # 数值主导块 (价格指标条/行情数字串): 信息全在数字里, 中文重写
            # 在窄格里必乱 (UxC 首页 "Ux Price Indicators" 乱码事故) → 不译不擦
            continue
        # 窄列标签 (块宽 < 2.5×字号, 原版即竖排单字母列): 中文排不进,
        # 译文必成竖排乱码 → 原样保留 (UxC 价格框 Conversion/UF6/EUP/SWU 竖标)
        _bb = b.get("bbox") or (0, 0, 0, 0)
        if (_bb[2] - _bb[0]) < 2.5 * (b.get("font_size") or 10.0):
            continue
        # 重叠保护: 本块红擦矩形会罩住窄列标签 → 跳过 (擦了会出半截残字)
        if _kills_narrow_label(fitz.Rect(*_bb)):
            continue
        trans = trans_map.get(b.get("block_id"))
        # 无译文 / keep-as-is (target==source): 不擦不写, 原样保留
        if not (trans and trans.target and trans.target.strip() != text):
            continue
        orig_size = b.get("font_size")
        # 标题 = 粗体多数决命中 且 块较短 (真标题 <120 字符; 全粗长段落
        # 按正文渲染, 防整段加粗), 或字号显著大于正文
        src_len = len((b.get("text") or "").strip())
        n_src_lines = max(1, (b.get("text") or "").count("\n") + 1)
        is_heading = (bool(b.get("bold")) and src_len <= 120) or (
            bool(body_size and orig_size and orig_size > body_size + 1.2))
        if not is_heading and body_size and orig_size and \
                abs(orig_size - body_size) < 1.2:
            font_size = body_size                     # 正文: 主导字号档
            min_size = 8.0
            render_bold = False
        elif is_heading:
            render_bold = True
            if (orig_size and body_size and orig_size < body_size - 0.5) \
                    or n_src_lines >= 2:
                # 小号粗标签 (价格框行标签 8-9pt) 或多行合并块 (解析层把价格框
                # 相邻行并进同一粗体块): 保持原字号不放大 — 放大会超宽换行
                # 压到下一行 (UxC 价格框错位事故两个成分)
                font_size = orig_size or 11.0
                min_size = max(6.5, (orig_size or 8.0) - 1.5)
            else:
                # 标题: 加粗展示, 且比正文放大一点 (原字号更大则用原字号)。
                # TT 新闻标题与正文同号仅加粗 (Arial-BoldMT), 必须靠 bold 标记识别。
                font_size = max(orig_size or 11.0, (body_size or 10.0) + 1.0)
                min_size = max(8.0, (body_size or 8.0) + 2.0)   # 降档不低于正文档+2
        else:
            font_size = orig_size                     # 脚注等: 原字号档
            min_size = 8.0
            render_bold = False
        # 原文行距 = 块高 / 原文行数 (钳到 [1.0, 1.35]); 译文按此行距摆行
        bx0, by0, bx1, by1 = bbox
        n_src = max(1, b.get("n_lines") or ((b.get("text") or "").count("\n") + 1))
        ref_fs = font_size or 11.0
        # 行距 = 抄原文行距, 但下限 1.2: 中文排版可读下限 (TT 原文行距多在
        # 1.1-1.15, 直接照抄中文观感偏挤; 模拟验证 lh≥1.2 零额外字号降档)
        lh = min(1.35, max(1.2, (by1 - by0) / n_src / ref_fs))
        text_jobs.append((trans.target, bbox, font_size,
                          b.get("color"), render_bold, min_size, lh,
                          bool(b.get("italic"))))
        page.add_redact_annot(fitz.Rect(*bbox))

    table_jobs: list[tuple] = []         # (tbl 对象, translated_table)
    if table_blocks:
        try:
            tf = page.find_tables()
            # 1.24 返回 TableFinder (有 .tables), 1.28 返回 list
            raw = tf.tables if hasattr(tf, "tables") else list(tf)
            # 与 parse_pdf 同一去重规则: 嵌套重复检出若不去掉, 同区域会写入两份译文
            from ..parsing.pdf_parser import dedupe_page_tables
            found = dedupe_page_tables(raw)
        except Exception as e:
            print(f"[bilingual_pdf] find_tables 失败: {e}")
            found = []
        for ordinal, block in enumerate(table_blocks):
            trans = trans_map.get(block.get("block_id"))
            if not (trans and trans.translated_table):
                continue
            if ordinal >= len(found):
                break
            page.add_redact_annot(fitz.Rect(*found[ordinal].bbox))
            table_jobs.append((found[ordinal], trans.translated_table))

    if not (text_jobs or table_jobs):
        return

    # 本页写字用字体子集: 治浏览器拒载 + 单页体积 (调用方已传则复用, 整本
    # 生成时传全书字符集的并集子集)
    if cjk_ff is None and CJK_FONTFILE:
        page_chars = "".join(t[0] for t in text_jobs)
        for _tbl, tt in table_jobs:
            page_chars += "".join(str(h or "") for h in (tt.get("headers") or []))
            page_chars += "".join(str(c or "") for row in (tt.get("rows") or [])
                                  for c in row)
        sub = _subset_font(CJK_FONTFILE, page_chars, "cjk")
        if sub:
            cjk_ff = sub
    if latin_ff is None and LATIN_FONTFILE:
        page_chars = "".join(t[0] for t in text_jobs)
        for _tbl, tt in table_jobs:
            page_chars += "".join(str(h or "") for h in (tt.get("headers") or []))
            page_chars += "".join(str(c or "") for row in (tt.get("rows") or [])
                                  for c in row)
        sub = _subset_font(LATIN_FONTFILE, page_chars, "latin")
        if sub:
            latin_ff = sub

    # 抹原文: 图片(IMAGE_NONE)与矢量线条(LINE_ART_NONE)一律不动 → 图表/网格线完好。
    # graphics 参数 PyMuPDF 1.24.x 起才支持, 旧版本只保图片。
    _line_art_none = getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0)
    try:
        try:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                  graphics=_line_art_none)
        except TypeError:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        # 显式删除残留 Redact 标注 (pdfjs 会把 redact 画成红色矩形)
        for a in list(page.annots() or []):
            if a.type[0] == 12:  # PDF_ANNOT_REDACT
                page.delete_annot(a)
    except Exception as e:
        print(f"[bilingual_pdf] 页 apply_redactions 失败: {e}")
        return

    # 每个文本块的扩框下限 = 横向有交叠的最近下方块顶边 (密集页防止扩框压块)
    all_boxes = [fitz.Rect(b["bbox"]) for b in blocks
                 if b.get("block_type", "text") == "text" and b.get("bbox")]

    def _next_block_top(bbox) -> float | None:
        x0, y0, x1, y1 = bbox
        top = None
        for r in all_boxes:
            if r.y0 >= y1 - 1 and not (r.x1 <= x0 or r.x0 >= x1):
                if top is None or r.y0 < top:
                    top = r.y0
        return top

    for target, bbox, font_size, color, bold, min_size, lh, italic in text_jobs:
        _draw_block_text(page, target, bbox, font_size, color, bold,
                         italic=italic,
                         max_bottom=_next_block_top(bbox), min_size=min_size,
                         lh=lh, cjk_ff=cjk_ff, latin_ff=latin_ff)

    for tbl, translated_table in table_jobs:
        _write_table_cells(page, tbl, translated_table,
                           cjk_ff=cjk_ff, latin_ff=latin_ff)


def generate_bilingual_pdf(report_id: str, report_obj, source: str = "file1") -> Path:
    """生成原位译 PDF。

    Args:
        report_id: 报告 id
        report_obj: Report 模型实例 (含 parsed 与 translations 缓存)
        source: 文件来源 file1/file2/file3 (周报双源/季报三源各生成独立译文 PDF)
    Returns:
        生成的 PDF 文件路径 ({report_id}[_2|_3]_bilingual.pdf)
    """
    if not report_obj.parsed:
        raise RuntimeError("报告尚未解析完成, 无法生成原位译 PDF")

    # 原 PDF 路径 (按 source 选对应文件)
    if source == "file2":
        src_file = report_obj.file_name_2
        src_pdf = UPLOAD_DIR / f"{report_id}_2_{src_file}"
    elif source == "file3":
        src_file = report_obj.file_name_3
        src_pdf = UPLOAD_DIR / f"{report_id}_3_{src_file}"
    else:
        src_file = report_obj.file_name
        src_pdf = UPLOAD_DIR / f"{report_id}_{src_file}"
    if not src_pdf.exists():
        raise RuntimeError(f"原 PDF 不存在: {src_pdf}")

    # 按 source 选 parsed (周报双源/季报三源各独立)
    parsed_obj = (report_obj.parsed_2 if source == "file2"
                  else report_obj.parsed_3 if source == "file3"
                  else report_obj.parsed)

    # 取按页分块结构
    page_blocks = get_page_blocks(parsed_obj)
    # 取译文缓存 (translate.py 模块级单例, 缓存键含 source)
    from ..routers.translate import _translation_cache, _cache_key
    cached_translations = _translation_cache.get(_cache_key(report_id, source), {})

    doc = fitz.open(str(src_pdf))

    # 整本共用一个子集: 先收集全书要写的字符并集, 一次子集化 (每页各建会
    # 内嵌 26 份不同子集, 下载体积翻倍)
    all_chars = ""
    for page_num, blocks in page_blocks.items():
        for t in cached_translations.get(page_num, []):
            if t is not None and getattr(t, "block_id", None):
                all_chars += (t.target or "")
        for b in blocks:
            if b.get("block_type") == "table":
                all_chars += "".join(str(h or "") for h in (b.get("headers") or []))
                all_chars += "".join(str(c or "") for row in (b.get("rows") or [])
                                     for c in row)
    full_cjk = _subset_font(CJK_FONTFILE, all_chars, "cjk") if CJK_FONTFILE else None
    full_latin = _subset_font(LATIN_FONTFILE, all_chars, "latin") if LATIN_FONTFILE else None

    for page_idx in range(len(doc)):
        page_num = page_idx + 1  # PyMuPDF 0-based, TextBlock.page 1-based
        if page_num not in page_blocks:
            continue
        page = doc[page_idx]
        blocks = page_blocks[page_num]
        page_translations = cached_translations.get(page_num, [])
        trans_map = {t.block_id: t for t in page_translations if t.block_id}
        render_page_bilingual(page, blocks, trans_map,
                              cjk_ff=full_cjk, latin_ff=full_latin)

    # 输出文件名按 source 区分 (file1 默认不带后缀, 兼容前端旧逻辑)
    src_suffix = "" if source == "file1" else f"_{source.replace('file','')}"
    out_path = UPLOAD_DIR / f"{report_id}{src_suffix}_bilingual.pdf"
    # 注: 不做 subset_fonts —— 实测 fitz 1.27 会破坏嵌入字体度量 (选择框全崩)
    doc.save(str(out_path), garbage=3, deflate=True)
    doc.close()
    print(f"[bilingual_pdf] 生成 {out_path}, 大小 {out_path.stat().st_size / 1024:.1f} KB")
    return out_path
