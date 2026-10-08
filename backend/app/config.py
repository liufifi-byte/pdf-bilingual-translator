"""应用配置。

模型层通过 LLM_BACKEND 切换:
- "stub": 占位实现, 本地开发用 (默认, 无需任何 key)
- "glm": 智谱云 API 或任何 OpenAI 兼容 endpoint (配置 .env 后启用真翻译)
"""
from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)   # 术语库 glossary.json 落盘目录
GLOSSARY_PATH = DATA_DIR / "glossary.json"   # 翻译术语库持久化文件

# 加载 .env (本地开发; 生产用环境变量注入)
_env_path = BASE_DIR / ".env"
if _env_path.exists():
    with _env_path.open() as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# 模型后端配置
LLM_BACKEND = os.getenv("LLM_BACKEND", "stub")

# GLM 配置 (智谱云或 OpenAI 兼容私有化部署)
GLM_BASE_URL = os.getenv("GLM_BASE_URL", "")  # 空 → GLMClient 自动用智谱云
GLM_API_KEY = os.getenv("GLM_API_KEY", "")
GLM_MODEL = os.getenv("GLM_MODEL", "glm-4")

# 翻译质量阈值
COMET_THRESHOLD = 0.85  # COMET < 0.85 触发人工复核
