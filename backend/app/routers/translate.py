"""按需翻译 + 后台全量 路由 (R1 翻译场景核心).

设计:
  上传 → 解析 → 后台全量任务 (一条线, 顺次翻所有页)
  
  用户在前台滚动 PDF → IntersectionObserver 监听 →
    先查缓存 (有则立即返回) → 未命中则前台插队翻这一页 (3-10s)
  
  缓存: 内存 dict {report_id: {page_num: [TranslationResult, ...]}}, 加锁

三个端点:
  POST /api/reports/{id}/translate-background   启动后台全量 (上传后自动触发)
  GET  /api/reports/{id}/page/{n}/translation    查此页译文缓存
  POST /api/reports/{id}/page/{n}/translate      按需触发翻译此页
"""
from __future__ import annotations
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from ..models import TranslationResult
from ..translation.pipeline import TranslationPipeline
from .upload import get_report_obj, _progress, _lock

router = APIRouter()

# 译文缓存: {(report_id, source): {page_num: [TranslationResult, ...]}}
_translation_cache: dict[tuple[str, str], dict[int, list[TranslationResult]]] = {}
# 正在翻译中的页集合: {(report_id, source): set(page_num)}
_in_progress: dict[tuple[str, str], set[int]] = {}
_cache_lock = threading.Lock()

_CONCURRENCY_BG = 2   # 降低到 2(OCR 占用 QPS 后翻译容易触发 429)
_CONCURRENCY_FG = 1


# 报告源语言检测结果缓存 {(report_id, source): 语言名}
_detected_src_lang: dict[tuple[str, str], str] = {}


def _resolve_src_lang(report_id: str, source: str, src_lang: str,
                      parsed) -> str:
    """解析源语言: 用户显式选择优先; "auto"/"自动"/空 → 检测报告实际语言。

    90%+ 报告为英文 (检测秒回); 小语种报告自动适配, 用户不用手动选。
    """
    if src_lang not in ("auto", "", "自动"):
        return src_lang
    ck = _cache_key(report_id, source)
    if ck not in _detected_src_lang:
        from ..parsing.pdf_parser import detect_document_language
        _detected_src_lang[ck] = detect_document_language(parsed)
    return _detected_src_lang[ck]


# 各 (report, source) 当前使用的翻译语言方向; 换语言 → 清缓存重译
_lang_used: dict[tuple[str, str], tuple[str, str]] = {}

# 译文 PDF 生成时使用的语言方向 {(report_id, source): (src_lang, tgt_lang)}
# (start-full-translation 据此判断已就绪的 PDF 是否为当前方向, 旧方向的作废重生成)
_pdf_lang: dict[tuple[str, str], tuple[str, str]] = {}

# AI 概括缓存 {(report_id, source): 概括文本}
_summary_cache: dict[tuple[str, str], str] = {}


def _ensure_lang(report_id: str, source: str,
                 src_lang: str, tgt_lang: str) -> None:
    """登记/校验翻译方向; 与已缓存方向不一致时清空该源的译文缓存。

    译文缓存不区分语言 —— 不做这一步, 换目标语言后会命中旧语言缓存。
    """
    ck = _cache_key(report_id, source)
    want = (src_lang, tgt_lang)
    used = _lang_used.get(ck)
    if used == want:
        return
    if used is not None:  # 同一报告换过语言 → 旧缓存作废
        print(f"[translate] 翻译方向变更 {used} → {want}, 清空缓存重译")
        with _cache_lock:
            _translation_cache.pop(ck, None)
        for k in [k for k in _page_done
                  if k[0] == report_id and k[1] == source]:
            _page_done.discard(k)
    _lang_used[ck] = want


def _page_failed(results: list) -> bool:
    """页翻译是否失败过半 (多为 GLM 限流/超时导致整页兜底回退原文)。

    失败页不写缓存、不打完成标 → 下次浏览/后台任务自动重译。
    否则"保留原文"的兜底会被烙进缓存, 用户看到大片英文以为翻完了。"""
    if not results:
        return False
    failed = sum(1 for r in results if getattr(r, "needs_review", False))
    return failed / len(results) > 0.4


def _cache_key(report_id: str, source: str = "file1") -> tuple[str, str]:
    return (report_id, source)


def _get_or_init_cache(report_id: str, source: str = "file1") -> dict[int, list[TranslationResult]]:
    ck = _cache_key(report_id, source)
    if ck not in _translation_cache:
        _translation_cache[ck] = {}
    if ck not in _in_progress:
        _in_progress[ck] = set()
    return _translation_cache[ck]


def _has_cache(report_id: str, page_num: int, source: str = "file1") -> bool:
    with _cache_lock:
        return page_num in _translation_cache.get(_cache_key(report_id, source), {})


def _set_cache(report_id: str, page_num: int,
               results: list[TranslationResult], source: str = "file1") -> None:
    with _cache_lock:
        _get_or_init_cache(report_id, source)
        _translation_cache[_cache_key(report_id, source)][page_num] = results


# 页完成标记 (流式翻译引入): 缓存里有该页 ≠ 该页翻完 — 流式发布会写"部分结果"
# 进缓存。done 判定只看此集合, 进度统计/缓存早退/渲染端点统一用它。
_page_done: set = set()


def _page_done_key(report_id: str, page_num: int, source: str) -> tuple:
    return (report_id, source, page_num)


def _is_page_done(report_id: str, page_num: int, source: str = "file1") -> bool:
    with _cache_lock:
        return _page_done_key(report_id, page_num, source) in _page_done


def _mark_page_done(report_id: str, page_num: int, source: str = "file1") -> None:
    with _cache_lock:
        _page_done.add(_page_done_key(report_id, page_num, source))


@router.post("/api/reports/{report_id}/start-translation")
def start_translation(report_id: str, source: str = "file1",
                      src_lang: str = "auto", tgt_lang: str = "中文"):
    """用户手动触发翻译（标记已启动, 实际按页懒翻译由前端驱动）。

    不起后台全量线程。前端 IntersectionObserver 按页调
    /page/{n}/translate 驱动单页翻译。
    src_lang/tgt_lang: 翻译方向在启动时登记 (换方向 → _ensure_lang 清旧缓存)。
    """
    report = get_report_obj(report_id)
    parsed = report.parsed
    if not parsed:
        raise HTTPException(400, "文档尚未解析完成")
    resolved_src = _resolve_src_lang(report_id, source, src_lang, parsed)
    _ensure_lang(report_id, source, resolved_src, tgt_lang)
    started_key = f"_translate_started_{source}"
    setattr(report, started_key, True)
    with _lock:
        _progress[report_id] = {"phase": "ready", "current": 0,
                                "total": parsed.num_pages,
                                "msg": "按页翻译模式（滑到哪页译哪页）"}
    return {"id": report_id, "background": "started",
            "source": source, "total_pages": parsed.num_pages,
            "src_lang": resolved_src, "tgt_lang": tgt_lang}


@router.get("/api/reports/{report_id}/detect-language")
def detect_language_endpoint(report_id: str, source: str = "file1"):
    """返回源文档自动检测的语言 (前端「源语言: 自动检测」框展示用)。"""
    report = get_report_obj(report_id)
    parsed = report.parsed
    if not parsed:
        raise HTTPException(400, "报告尚未解析完成")
    lang = _resolve_src_lang(report_id, source, "auto", parsed)
    return {"id": report_id, "source": source, "src_lang": lang}


# AI 概括正文输入上限: 超长文档取「头 3/4 + 尾 1/4」(行业报告结论常在后部)
_SUMMARY_CHAR_LIMIT = 80000


@router.post("/api/reports/{report_id}/ai-summary")
def ai_summary(report_id: str, source: str = "file1", refresh: bool = False):
    """源文档 AI 概括 (前端顶部「AI 概括」按钮)。

    同步调用 LLM (glm-5.3-flash 关思考档 ~10-30s, 前端按钮转 loading);
    结果按 (report, source) 缓存, refresh=true 强制重新生成。
    """
    report = get_report_obj(report_id)
    parsed = report.parsed
    if not parsed:
        raise HTTPException(400, "报告尚未解析完成")

    ck = _cache_key(report_id, source)
    if not refresh and ck in _summary_cache:
        return {"summary": _summary_cache[ck], "cached": True}

    # 拼接正文: 跳过页眉/图片内/图表内块 (与翻译跳过口径一致, 只概括正文)
    parts: list[str] = []
    for tb in parsed.text_blocks:
        t = (tb.text or "").strip()
        if not t or tb.in_image or tb.in_chart or tb.is_header:
            continue
        parts.append(t)
    for tbl in parsed.table_blocks:
        cap = (tbl.caption or "").strip()
        if cap:
            parts.append(cap)
    full = "\n".join(parts)
    if not full.strip():
        raise HTTPException(400, "文档没有可概括的正文文本")
    if len(full) > _SUMMARY_CHAR_LIMIT:
        head = _SUMMARY_CHAR_LIMIT * 3 // 4
        tail = _SUMMARY_CHAR_LIMIT - head
        full = full[:head] + "\n…(中间部分省略)…\n" + full[-tail:]

    src_lang = _resolve_src_lang(report_id, source, "auto", parsed)
    from ..llm.client import get_llm_client
    summary = get_llm_client().summarize_document(full, src_lang)
    _summary_cache[ck] = summary
    return {"summary": summary, "cached": False, "src_lang": src_lang}


@router.post("/api/reports/{report_id}/start-full-translation")
def start_full_translation(report_id: str, source: str = "file1",
                           src_lang: str = "auto", tgt_lang: str = "中文"):
    """触发全文翻译 (用户点「下载译文」按钮调)。

    - 若该 source 的译文 PDF 已生成 → 直接返回 ready=true, 前端可下载
    - 若已在翻译中 → 返回进行中状态, 前端继续轮询
    - 否则起后台线程全量翻译 + 生成 bilingual PDF, 立即返回 'pending'
    """
    report = get_report_obj(report_id)
    parsed = report.parsed
    if not parsed:
        raise HTTPException(400, "报告尚未解析完成")

    # 登记翻译方向: 换方向 → _ensure_lang 自动清空旧语言的译文缓存/完成标
    resolved_src = _resolve_src_lang(report_id, source, src_lang, parsed)
    _ensure_lang(report_id, source, resolved_src, tgt_lang)

    # 已生成译文 PDF 且就是当前方向 → 直接告诉前端可下载;
    # 旧方向生成的 PDF 作废 (不重新生成, 后台线程重新翻)
    ck = _cache_key(report_id, source)
    pdf_path_attr = f"bilingual_pdf_path{_source_suffix(source)}"
    if getattr(report, pdf_path_attr, None):
        if _pdf_lang.get(ck) == (resolved_src, tgt_lang):
            return {"id": report_id, "source": source, "state": "ready",
                    "total_pages": parsed.num_pages}
        print(f"[translate] 译文 PDF 为旧语言方向 {_pdf_lang.get(ck)}, "
              f"按新方向 ({resolved_src}→{tgt_lang}) 重新生成")
        setattr(report, pdf_path_attr, None)

    # 已在翻译中 → 返回进行中
    with _lock:
        prog = _progress.get(_full_progress_key(report_id, source), {})
    if prog.get("phase") == "translating":
        return {"id": report_id, "source": source, "state": "translating",
                "current": prog.get("current", 0),
                "total": prog.get("total", parsed.num_pages),
                "msg": prog.get("msg", "")}

    # 否则启动后台全量翻译 (daemon 线程, 不阻塞当前请求)
    import threading
    t = threading.Thread(target=_background_translate_all,
                         args=(report_id, source, src_lang, tgt_lang), daemon=True)
    t.start()
    return {"id": report_id, "source": source, "state": "started",
            "total_pages": parsed.num_pages}


@router.get("/api/reports/{report_id}/full-translation-progress")
def full_translation_progress(report_id: str, source: str = "file1"):
    """查询全文翻译进度 (下载前前端轮询用)。"""
    report = get_report_obj(report_id)
    parsed = report.parsed
    total = parsed.num_pages if parsed else 0
    pdf_path_attr = f"bilingual_pdf_path{_source_suffix(source)}"
    pdf_ready = getattr(report, pdf_path_attr, None) is not None
    with _lock:
        prog = _progress.get(_full_progress_key(report_id, source), {})
    return {
        "id": report_id, "source": source,
        "state": "ready" if pdf_ready else prog.get("phase", "idle"),
        "current": prog.get("current", 0),
        "total": total,
        "msg": prog.get("msg", ""),
        "bilingual_pdf_ready": pdf_ready,
    }


@router.get("/api/reports/{report_id}/bilingual-pdf")
def download_bilingual_pdf(report_id: str, source: str = "file1"):
    """下载译文 PDF (完整中英对照, 用户点下载译文按钮用)。

    若未生成 → 404, 前端应先调 start-full-translation。
    """
    from fastapi.responses import FileResponse
    report = get_report_obj(report_id)
    pdf_path_attr = f"bilingual_pdf_path{_source_suffix(source)}"
    path_str = getattr(report, pdf_path_attr, None)
    if not path_str:
        raise HTTPException(404, "译文 PDF 尚未生成, 请先触发全文翻译")
    path = Path(path_str)
    if not path.exists():
        raise HTTPException(404, "译文 PDF 文件不存在")
    src_file = report.file_name
    download_name = f"译文_{src_file}" if src_file else path.name
    return FileResponse(path, media_type="application/pdf", filename=download_name,
                        headers={"Cache-Control": "no-store"})


def _background_translate_all(report_id: str, source: str = "file1",
                              src_lang: str = "auto",
                              tgt_lang: str = "中文") -> None:
    """后台全量翻译 (用户点「下载译文」触发)。

    逐页翻译, 每页走 translate_page_batch (批量合并流水线),
    翻完后生成 {report_id}[_2|_3]_bilingual.pdf 供下载。
    所有页并发执行 (避免长尾), 进度按 done/total 更新。
    """
    from ..parsing.pdf_parser import get_page_blocks

    report = get_report_obj(report_id)
    parsed = report.parsed
    if not parsed:
        return

    page_blocks = get_page_blocks(parsed)
    total_pages = parsed.num_pages
    pipeline = TranslationPipeline()

    def _translate_one_page(page_num: int) -> None:
        if _is_page_done(report_id, page_num, source):
            return
        try:
            blocks = page_blocks.get(page_num, [])
            if not blocks:
                _set_cache(report_id, page_num, [], source)
                _mark_page_done(report_id, page_num, source)
                return
            # ⚠️ 不能写 `src_lang = ...`: 对闭包外层参数同名赋值会把它变成
            # 局部变量, 右侧再读即 UnboundLocalError → 每页翻译秒失败被
            # except 吞掉 (2026-09-08 事故: 全文翻译 90s "完成"但零中文)
            resolved_src = _resolve_src_lang(report_id, source, src_lang, parsed)
            _ensure_lang(report_id, source, resolved_src, tgt_lang)
            results = pipeline.translate_page_batch(page_num, blocks,
                                                    src_lang=resolved_src, tgt_lang=tgt_lang)
            if _page_failed(results):
                print(f"[translate] 后台 p{page_num} 失败过半 (限流?), 不缓存待重译")
            else:
                _set_cache(report_id, page_num, results, source)
                _mark_page_done(report_id, page_num, source)
        except Exception as e:
            _set_cache(report_id, page_num, [TranslationResult(
                source="", target=f"[翻译失败: {e}]", page=page_num)], source)
            _mark_page_done(report_id, page_num, source)

    with _lock:
        report.status = "translating"
        _progress[_full_progress_key(report_id, source)] = {
            "phase": "translating", "current": 0,
            "total": total_pages, "msg": "全文翻译中..."}

    # 并发翻译所有页 (按 _CONCURRENCY_BG 控制并发度)
    done = 0
    page_nums = list(page_blocks.keys())
    with ThreadPoolExecutor(max_workers=_CONCURRENCY_BG) as pool:
        for _ in pool.map(_translate_one_page, page_nums):
            done += 1
            with _lock:
                _progress[_full_progress_key(report_id, source)] = {
                    "phase": "translating", "current": done,
                    "total": total_pages,
                    "msg": f"全文翻译 {done}/{total_pages} 页"}

    with _lock:
        _progress[_full_progress_key(report_id, source)] = {
            "phase": "rendering", "current": total_pages,
            "total": total_pages,
            "msg": "正在合并下载 PDF..."}

    # 翻完后生成原位译 PDF 供下载
    try:
        from ..translation.bilingual_pdf import generate_bilingual_pdf
        out_path = generate_bilingual_pdf(report_id, report, source)
        # path 存到 source 化字段 (file1→bilingual_pdf_path, file2→bilingual_pdf_path_2 ...)
        setattr(report, f"bilingual_pdf_path{_source_suffix(source)}", str(out_path))
        # 记录本次 PDF 的语言方向 (换语言再来下载时据此作废旧 PDF)
        _pdf_lang[_cache_key(report_id, source)] = (
            _resolve_src_lang(report_id, source, src_lang, parsed), tgt_lang)
        with _lock:
            _progress[_full_progress_key(report_id, source)] = {
                "phase": "done", "current": total_pages, "total": total_pages,
                "msg": "译文 PDF 已生成"}
    except Exception as e:
        print(f"[translate] 生成原位译 PDF 失败: {e}")
        with _lock:
            _progress[_full_progress_key(report_id, source)] = {
                "phase": "failed", "current": total_pages, "total": total_pages,
                "msg": f"译 PDF 生成失败: {e}"}


def _source_suffix(source: str) -> str:
    """file1→'', file2→'_2', file3→'_3' (字段命名用)。"""
    return "" if source == "file1" else f"_{source.replace('file','')}"


def _full_progress_key(report_id: str, source: str) -> tuple[str, str]:
    """全文翻译进度键 (与单页 _progress 区分, 单源隔离)。"""
    return (report_id, source)


def _update_bg_progress(report_id: str, done: int, total: int) -> None:
    with _lock:
        _progress[report_id] = {
            "phase": "translating",
            "current": done,
            "total": total,
            "msg": f"后台翻译 {done}/{total} 页",
        }


@router.get("/api/reports/{report_id}/page/{page_num}/translation")
def get_page_translation(report_id: str, page_num: int, source: str = "file1",
                         src_lang: str = "auto", tgt_lang: str = "中文"):
    """流式视图数据源 (2026-08-21): 该页待译块浮层元数据 + 已完成译文 + 完成标志。

    前端右侧"原文画布 + DOM 浮层"用: 画布只渲染一次, 本接口 1s 轮询 —
    blocks 按揭示顺序 (阅读顺序) 给出, translated 是已完成块的 block_id→译文,
    前端按顺序翻亮 (某块译好但要等前序块都好了才揭示, 视觉=从上到下逐段翻)。
    未开始翻译的页自动起流式线程 (与 /page/{n}/bilingual 同一套)。
    """
    from ..parsing.pdf_parser import get_page_blocks
    from ..translation.pipeline import classify_page_blocks

    report = get_report_obj(report_id)
    parsed = report.parsed
    if not parsed:
        raise HTTPException(400, "报告尚未解析完成")

    done = _is_page_done(report_id, page_num, source)
    if not done:
        ck = _cache_key(report_id, source)
        with _cache_lock:
            inflight = page_num in _in_progress.get(ck, set())
        if not inflight:
            import threading
            threading.Thread(target=_translate_page_sync,
                             args=(report_id, page_num, source),
                             kwargs={"src_lang": src_lang, "tgt_lang": tgt_lang},
                             daemon=True).start()

    blocks = get_page_blocks(parsed).get(page_num, [])
    cls = classify_page_blocks(page_num, blocks)
    with _cache_lock:
        page_results = _translation_cache.get(_cache_key(report_id, source), {}).get(page_num, [])
    translated = {r.block_id: r.target for r in page_results
                  if r is not None and getattr(r, "block_id", None)
                  and getattr(r, "target", None)}
    return {"page": page_num, "done": done, "blocks": cls["overlay"],
            "body_size": cls.get("body_size"),
            "translated": translated}


@router.post("/api/reports/{report_id}/page/{page_num}/translate")
def translate_single_page(report_id: str, page_num: int, source: str = "file1",
                           force: bool = False,
                           src_lang: str = "auto", tgt_lang: str = "中文"):
    """前台按需翻译某单页 (用户滑到此页时调用)。

    force=False (默认): 命中缓存直接返回旧译文 (其他调用方零影响)。
    force=True: 跳过缓存早退, 强制重译该页 (用户术语库改动后点「重译此页」用)。
    """
    from ..parsing.pdf_parser import get_page_blocks

    report = get_report_obj(report_id)
    parsed = report.parsed
    if not parsed:
        raise HTTPException(400, "报告尚未解析完成")

    if not force and _is_page_done(report_id, page_num, source):
        with _cache_lock:
            page_results = _translation_cache[_cache_key(report_id, source)].get(page_num, [])
        return {"page": page_num, "results": [r.model_dump() for r in page_results],
                "source": "cache"}

    with _cache_lock:
        _get_or_init_cache(report_id, source)
        ck = _cache_key(report_id, source)
        if page_num in _in_progress[ck]:
            return {"page": page_num, "results": [], "source": "in-progress"}
        _in_progress[ck].add(page_num)

    try:
        page_blocks = get_page_blocks(parsed)
        if page_num not in page_blocks:
            results = []
            _set_cache(report_id, page_num, results, source)
            _mark_page_done(report_id, page_num, source)
            return {"page": page_num, "results": [], "source": "empty-page"}

        blocks = page_blocks[page_num]
        pipeline = TranslationPipeline()
        src_lang = _resolve_src_lang(report_id, source, src_lang, parsed)
        _ensure_lang(report_id, source, src_lang, tgt_lang)
        # 与批量路径/前台单页 PDF 完全同一实现 (两条路径共享缓存, 行为必须一致)
        results = pipeline.translate_page_batch(page_num, blocks,
                                                src_lang=src_lang, tgt_lang=tgt_lang)
        if _page_failed(results):
            print(f"[translate] p{page_num} 前台翻译失败过半, 不缓存待重译")
            return {"page": page_num, "results": [r.model_dump() for r in results],
                    "source": "failed-retry-later"}
        _set_cache(report_id, page_num, results, source)
        _mark_page_done(report_id, page_num, source)
        return {"page": page_num, "results": [r.model_dump() for r in results],
                "source": "fresh"}
    finally:
        with _cache_lock:
            _in_progress[_cache_key(report_id, source)].discard(page_num)


def _translate_page_sync(report_id: str, page_num: int, source: str = "file1",
                         force: bool = False,
                         src_lang: str = "auto", tgt_lang: str = "中文"):
    """翻译单页 (流式, 2026-08-21) — 分批发布 + 完成标记。

    与旧版的区别: 每批 LLM 结果完成即写缓存 (publish 回调), 前端轮询
    /page/{n}/bilingual 就能看到译文逐批上页; 全部完成后打 _page_done 标。
    _in_progress 集合防同页并发双跑。
    """
    from ..parsing.pdf_parser import get_page_blocks

    key = _page_done_key(report_id, page_num, source)
    if not force and _is_page_done(report_id, page_num, source):
        return
    ck = _cache_key(report_id, source)
    with _cache_lock:
        _get_or_init_cache(report_id, source)
        if page_num in _in_progress[ck]:
            return
        _in_progress[ck].add(page_num)
    try:
        report = get_report_obj(report_id)
        parsed = report.parsed
        if not parsed:
            return
        page_blocks = get_page_blocks(parsed)
        blocks = page_blocks.get(page_num, [])
        if not blocks:
            _set_cache(report_id, page_num, [], source)
            _mark_page_done(report_id, page_num, source)
            return
        pipeline = TranslationPipeline()

        def _publish(partial: list) -> None:
            _set_cache(report_id, page_num, partial, source)

        src_lang = _resolve_src_lang(report_id, source, src_lang, parsed)
        _ensure_lang(report_id, source, src_lang, tgt_lang)
        results = pipeline.translate_page_batch(page_num, blocks, publish=_publish,
                                                src_lang=src_lang, tgt_lang=tgt_lang)
        if _page_failed(results):
            # 失败过半: 撤完成标并清半成品缓存, 前端下次轮询自动重译
            print(f"[translate] p{page_num} 流式翻译失败过半, 撤销完成标待重译")
            with _cache_lock:
                _translation_cache.get(_cache_key(report_id, source), {}).pop(page_num, None)
                _page_done.discard(_page_done_key(report_id, page_num, source))
            return
        _set_cache(report_id, page_num, results, source)
        _mark_page_done(report_id, page_num, source)
    except Exception as e:
        print(f"[translate] 第{page_num}页流式翻译失败: {e}")
        # 失败也打完成标: 让前端轮询终止 (渲染会保留已完成部分)
        _mark_page_done(report_id, page_num, source)
    finally:
        with _cache_lock:
            _in_progress[ck].discard(page_num)


@router.get("/api/reports/{report_id}/page/{page_num}/bilingual")
def get_page_bilingual(report_id: str, page_num: int, source: str = "file1",
                       src_lang: str = "auto", tgt_lang: str = "中文"):
    """按需生成单页原位译 PDF (保留原文排版)。

    查该页译文缓存 → 未命中则同步翻译 → PyMuPDF 渲染单页 → 返回 PDF 二进制。
    """
    import fitz
    from fastapi.responses import Response
    from ..config import UPLOAD_DIR

    report = get_report_obj(report_id)
    parsed = report.parsed
    if not parsed:
        raise HTTPException(400, "报告尚未解析完成")

    # 1. 页完成则直接渲染终稿; 未完成且无在途任务 → 起后台流式翻译线程
    #    (分批发布缓存), 本请求立即返回当前进度的渲染 (流式翻译, 2026-08-21)
    done = _is_page_done(report_id, page_num, source)
    if not done:
        ck = _cache_key(report_id, source)
        with _cache_lock:
            inflight = page_num in _in_progress.get(ck, set())
        if not inflight:
            import threading
            threading.Thread(target=_translate_page_sync,
                             args=(report_id, page_num, source),
                             daemon=True).start()

    with _cache_lock:
        results = _translation_cache.get(_cache_key(report_id, source), {}).get(page_num, [])

    # 2. 原单页 PDF 到新文档
    src_name = f"{report_id}_{report.file_name}"
    src_path = UPLOAD_DIR / src_name
    if not src_path.exists():
        raise HTTPException(404, "原 PDF 文件不存在")

    src_doc = fitz.open(str(src_path))
    page_idx = page_num - 1
    if page_idx >= len(src_doc):
        src_doc.close()
        raise HTTPException(400, f"页码超出范围: {page_num}")

    new_doc = fitz.open()
    new_doc.insert_pdf(src_doc, from_page=page_idx, to_page=page_idx)
    src_doc.close()
    page = new_doc[0]

    # 3. 原位译渲染 (与整本下载 PDF 共用同一实现, 保证两出口效果一致)
    from ..translation.bilingual_pdf import render_page_bilingual
    from ..parsing.pdf_parser import get_page_blocks

    page_blocks = get_page_blocks(parsed)
    blocks = page_blocks.get(page_num, [])
    trans_map = {t.block_id: t for t in results if t.block_id}
    render_page_bilingual(page, blocks, trans_map)

    # 4. 输出单页 PDF bytes (no-store: 防浏览器/pdfjs 缓存旧版单页译);
    #    X-Page-Done 头告知前端该页是否翻译完毕 (未完则前端轮询刷新)
    pdf_bytes = new_doc.tobytes(deflate=True)
    new_doc.close()

    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Cache-Control": "no-store",
                             "X-Page-Done": "1" if done else "0"})


@router.get("/api/reports/{report_id}/translation-progress")
def translation_progress(report_id: str, source: str = "file1"):
    """获取翻译进度 (前台进度条用) + 已译页码列表。"""
    report = get_report_obj(report_id)
    with _lock:
        prog = _progress.get(report_id, {})
    with _cache_lock:
        # 已译页码 = 打了完成标的页 (流式发布会让部分完成的页也进缓存,
        # 只统计翻完的, 否则进度条虚高)
        ck = _cache_key(report_id, source)
        translated_pages = sorted(
            p for (rid, src, p) in _page_done
            if rid == report_id and src == source and p in _translation_cache.get(ck, {}))
    parsed = report.parsed
    total_pages = parsed.num_pages if parsed else 0
    return {
        "id": report_id,
        "done": len(translated_pages),
        "total": total_pages,
        "translated_pages": translated_pages,
        "phase": prog.get("phase", ""),
        "msg": prog.get("msg", ""),
        "bilingual_pdf_ready": getattr(
            report, f"bilingual_pdf_path{_source_suffix(source)}", None) is not None,
    }
