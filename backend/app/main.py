"""FastAPI 应用入口。

启动: uvicorn app.main:app --port 8000
"""
import os

# 本机系统代理(Clash 等)会被 httpx trust_env 静默继承, 导致 LLM 请求绕道代理
# 后挂死/超时。后端只访问配置的模型服务, 一律直连;
# setdefault 保证外部显式设置仍可覆盖。
os.environ.setdefault("NO_PROXY", "open.bigmodel.cn")
os.environ.setdefault("no_proxy", "open.bigmodel.cn")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .routers import upload, translate, glossary
from .config import UPLOAD_DIR

app = FastAPI(title="PDF 双语翻译", version="1.0.0")

# CORS (前端开发用; 生产环境按域名收紧)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态 PDF 访问 (前端 pdf.js 加载原 PDF 用)
app.mount("/files", StaticFiles(directory=str(UPLOAD_DIR)), name="files")

# 路由
app.include_router(upload.router)
app.include_router(translate.router)
app.include_router(glossary.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "pdf-bilingual-translator"}
