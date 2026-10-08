"""上传路由 (仅翻译场景)。

POST /api/upload  上传 PDF → 后台解析
GET  /api/reports 文档列表
GET  /api/reports/{id} 单个文档详情
GET  /api/reports/{id}/status 进度查询
"""
from __future__ import annotations
import threading
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse

from ..models import Report
from ..config import UPLOAD_DIR
from ..parsing.pdf_parser import parse_pdf

router = APIRouter()

# 内存存储 (MVP; 生产换数据库)
_reports: dict[str, Report] = {}
_progress: dict[str, dict] = {}
_lock = threading.Lock()


def _process_report(report_id: str, file_path: Path) -> None:
    """后台线程: 异步解析 PDF。解析完成即可进入翻译视图。"""
    report = _reports[report_id]
    try:
        with _lock:
            report.status = "parsing"
            _progress[report_id] = {"phase": "parsing", "current": 0, "total": 1,
                                    "msg": "解析 PDF 中..."}
        parsed = parse_pdf(file_path)
        report.parsed = parsed
        with _lock:
            report.status = "done"
            _progress[report_id] = {"phase": "done", "current": 1, "total": 1,
                                    "msg": "解析完成，可按页翻译或下载译文"}
    except Exception as e:
        with _lock:
            report.status = "failed"
            report.error = str(e)
            _progress[report_id] = {"phase": "failed", "current": 0, "total": 1,
                                    "msg": f"失败: {e}"}


@router.post("/api/upload")
async def upload_report(file: UploadFile = File(...)):
    """上传单份 PDF 文档, 后台解析后进入翻译视图。"""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "仅支持 PDF 文件")

    report_id = uuid.uuid4().hex[:12]
    saved_path = UPLOAD_DIR / f"{report_id}_{file.filename}"
    content = await file.read()
    saved_path.write_bytes(content)

    report = Report(
        id=report_id,
        file_name=file.filename,
        upload_time=datetime.now(),
        status="parsing",
        report_type="translation",
    )
    with _lock:
        _reports[report_id] = report
        _progress[report_id] = {"phase": "queued", "current": 0, "total": 1,
                                "msg": "已排队..."}

    t = threading.Thread(target=_process_report,
                         args=(report_id, saved_path), daemon=True)
    t.start()

    return JSONResponse({"id": report_id, "status": "parsing",
                         "file_name": file.filename, "error": ""})


@router.get("/api/reports/{report_id}/status")
def get_progress(report_id: str):
    """查询处理进度 (前端轮询用)。"""
    if report_id not in _reports:
        raise HTTPException(404, "文档不存在")
    with _lock:
        report = _reports[report_id]
        prog = _progress.get(report_id, {})
    return {
        "id": report_id,
        "status": report.status,
        "error": report.error,
        "phase": prog.get("phase", ""),
        "current": prog.get("current", 0),
        "total": prog.get("total", 1),
        "msg": prog.get("msg", ""),
    }


@router.get("/api/reports")
def list_reports():
    items = sorted(_reports.values(), key=lambda r: r.upload_time, reverse=True)
    return [{"id": r.id, "file_name": r.file_name,
             "upload_time": r.upload_time.isoformat(),
             "status": r.status, "report_type": r.report_type,
             "error": r.error} for r in items]


@router.get("/api/reports/{report_id}")
def get_report(report_id: str):
    if report_id not in _reports:
        raise HTTPException(404, "文档不存在")
    r = _reports[report_id]
    return r.model_dump()


def get_report_obj(report_id: str) -> Report:
    if report_id not in _reports:
        raise HTTPException(404, "文档不存在")
    return _reports[report_id]
