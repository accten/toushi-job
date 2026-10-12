"""
用户上传 resume.pdf
      ↓
→ 生成文件 hash（MD5）
      ↓
→ 查本地向量库是否已有对应 hash 的目录
      ↓
   ↙                ↘
已存在             不存在 or hash 变了
  ↓                        ↓
加载已有向量库      → 重新向量化 → 保存
"""

import hashlib
import os
import sys
from pathlib import Path

from toushi.paths import app_data_dir

# 嵌入模型的 HuggingFace 仓库 id（本地缓存目录约定：hub/<repo_id>/snapshots/<hash>/）
EMBED_MODEL_REPO_ID = "models--sentence-transformers--all-mpnet-base-v2"
# 显式覆盖：指向任意一份模型缓存根目录（下面几种布局都认，见 find_model_snapshot）
MODEL_DIR_ENV = "BOSS_MODEL_DIR"


def _candidate_model_roots() -> list[Path]:
    """按优先级列出所有可能的 models 根目录。

    为什么不写死一个路径：同一份代码要跑在三种布局下 ——
    - 开发：仓库根 models/
    - 安装包：跟嵌入式解释器同级的安装目录 models/（bundle resources 落这儿）
    - 用户数据目录：%APPDATA%/<identifier>/models（standalone 会把 CWD 切到那儿）

    老实现用 Path(__file__).resolve().parents[2] 反推仓库根：在源码树里正好对，
    装成 wheel 之后却是 Lib/，而 local_files_only=True 又不给联网兜底，
    结果就是「App 能开能填表，一点开始生成招呼语就崩」。
    """
    roots: list[Path] = []

    override = os.environ.get(MODEL_DIR_ENV, "").strip()
    if override:
        roots.append(Path(override))

    # 安装后：resources 解到安装目录，嵌入式解释器就在那儿
    try:
        roots.append(Path(sys.executable).resolve().parent / "models")
    except Exception:
        pass

    # 安装后：standalone 入口把 CWD 切到应用数据目录；开发时 CWD 是仓库根
    roots.append(Path.cwd() / "models")

    # 用户数据目录：允许用户自己塞一份（不依赖 CWD）
    try:
        roots.append(app_data_dir() / "models")
    except Exception:
        pass

    # 开发：仓库根（src/toushi/vectorization.py 往上两级）
    roots.append(Path(__file__).resolve().parents[2] / "models")

    # 去重但保持优先级
    seen: set[str] = set()
    unique: list[Path] = []
    for r in roots:
        key = str(r)
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def _hf_home() -> Path:
    """HF_HOME 定成哪：优先挑一个已经存在的候选根，都不存在就退回 CWD/models。"""
    for root in _candidate_model_roots():
        if root.is_dir():
            return root
    return Path.cwd() / "models"


# 必须在 import sentence_transformers / transformers 之前设好：它们读 env 的时机是导入期。
if "HF_HOME" not in os.environ:
    os.environ["HF_HOME"] = str(_hf_home())

import chromadb
from sentence_transformers import SentenceTransformer

COLLECTION_NAME = "resume"

_embedder: SentenceTransformer | None = None
_embedder_path: str | None = None


def _snapshot_dirs_under(root: Path) -> list[Path]:
    """一个根目录下所有可能的模型快照目录。兼容几种打包布局：
    hub/<repo>/snapshots/<hash>/ 、 <repo>/snapshots/<hash>/ 、 snapshots/<hash>/ 。
    """
    found: list[Path] = []
    snapshots = [
        root / "hub" / EMBED_MODEL_REPO_ID / "snapshots",
        root / EMBED_MODEL_REPO_ID / "snapshots",
        root / "snapshots",
    ]
    for parent in snapshots:
        if parent.is_dir():
            found.extend(sorted(p for p in parent.iterdir() if p.is_dir()))
    return found


def find_model_snapshot() -> Path | None:
    """找到可用的本地模型快照目录；找不到返回 None。

    判据是 config.json（SentenceTransformer 加载必需的那份）。
    """
    for root in _candidate_model_roots():
        if not root.is_dir():
            continue
        # 布局 A/B/C：snapshots/<hash>/
        for snap in _snapshot_dirs_under(root):
            if (snap / "config.json").is_file():
                return snap
        # 布局 D：模型文件直接摊在根目录（打包时把快照内容平铺的情况）
        if (root / "config.json").is_file() and (root / "sentence_bert_config.json").is_file():
            return root
    return None


def _missing_model_message() -> str:
    searched = "\n".join(f"      - {r}" for r in _candidate_model_roots())
    return (
        "找不到本地嵌入模型 all-mpnet-base-v2，无法把简历向量化。\n"
        f"  已找过这些位置：\n{searched}\n"
        "  解决办法（任选其一）：\n"
        f"    1) 设环境变量 {MODEL_DIR_ENV} 指向模型根目录（其下应有 "
        f"hub/{EMBED_MODEL_REPO_ID}/snapshots/<hash>/config.json）\n"
        "    2) 把模型拷到上面任意一个位置\n"
        "    3) 源码运行：确认仓库根目录下存在 models/（见 README 的「嵌入模型」一节）"
    )


def _get_embedder() -> SentenceTransformer:
    global _embedder, _embedder_path
    if _embedder is None:
        snapshot = find_model_snapshot()
        if snapshot is None:
            raise RuntimeError(_missing_model_message())
        _embedder = SentenceTransformer(str(snapshot), local_files_only=True)
        _embedder_path = str(snapshot)
    return _embedder


def text_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def split_text(text: str, chunk_size: int = 1000, chunk_overlap: int = 200) -> list[str]:
    if chunk_size <= chunk_overlap:
        raise ValueError("chunk_size must exceed chunk_overlap")
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end == n:
            break
        start = end - chunk_overlap
    return chunks


class VectorStore:
    """Chroma collection wrapper with semantic search over PDF chunks."""

    def __init__(self, collection: chromadb.Collection):
        self._collection = collection

    def search(self, query: str, k: int = 4) -> list[str]:
        query_embedding = _get_embedder().encode([query]).tolist()
        results = self._collection.query(query_embeddings=query_embedding, n_results=k)
        documents = results.get("documents") or []
        return documents[0] if documents else []

    def check_relevance(self, query: str, distance_threshold: float = 1.3) -> tuple[bool, float]:
        """检查查询（如JD）与集合的最短距离。
        distance_threshold 默认1.3（经验值，基于 L2 距离，越小越相似）。
        如果所有 chunk 的距离都大于阈值，说明完全不相关，返回 False。
        """
        query_embedding = _get_embedder().encode([query]).tolist()
        results = self._collection.query(query_embeddings=query_embedding, n_results=1)
        distances = results.get("distances")
        if not distances or not distances[0]:
            return True, 0.0  # 没提取到 distance 就放行
        
        min_distance = distances[0][0]
        return min_distance <= distance_threshold, min_distance


def embed_resume(resume_text: str, base_dir: str = "./vectorstores") -> VectorStore:
    file_id = text_hash(resume_text)
    persist_dir = Path(base_dir) / file_id
    persist_dir.mkdir(parents=True, exist_ok=True)

    client = chromadb.PersistentClient(path=str(persist_dir))
    existing = {c.name for c in client.list_collections()}

    if COLLECTION_NAME in existing:
        print("[OK] 加载已存在向量库")
        collection = client.get_collection(COLLECTION_NAME)
    else:
        print("[WARN] 不存在向量库，重新向量化")
        chunks = split_text(resume_text)
        if not chunks:
            raise ValueError("No text extracted from resume")

        embeddings = _get_embedder().encode(chunks).tolist()
        collection = client.create_collection(COLLECTION_NAME)
        collection.add(
            documents=chunks,
            embeddings=embeddings,
            ids=[f"chunk-{i}" for i in range(len(chunks))],
        )
        print(f"[OK] 已保存向量库到：{persist_dir}")

    return VectorStore(collection)
