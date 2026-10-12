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

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Callable, Optional

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




# ---------- 模型下载（首次安装后按需拉取） ----------
#
# 安装包不再内置模型（418MB，占整个包的三分之一还多），改成用户点一下再下。
# 下到 model_download_dir()，也就是 _candidate_model_roots() 的候选之一，
# 所以下完 find_model_snapshot() 立刻就能找到，不需要重启。

# HuggingFace 上的仓库 id（注意不是本地缓存目录名，那个带 models-- 前缀）
EMBED_MODEL_HF_REPO = "sentence-transformers/all-mpnet-base-v2"
# 固定 revision，保证每个人装到的权重完全一致（也顺便是一个稳定的下载地址）
EMBED_MODEL_REVISION = "e8c3b32edf5434bc2275fc9bab85f82640a19130"
# 只拉推理要的文件。不加过滤会把 pytorch_model.bin / onnx / tf 变体一起拖下来，
# 体积能翻两三倍——这条跟打包脚本里的 --include 是同一个理由。
EMBED_MODEL_ALLOW_PATTERNS = ["*.json", "*.txt", "model.safetensors"]
# 给 UI 显示的预估体积（实测约 418MB，取整）
EMBED_MODEL_APPROX_MB = 420


def model_download_dir() -> Path:
    """模型下载到哪：BOSS_MODEL_DIR 优先，否则应用数据目录下的 models/。

    必须是 _candidate_model_roots() 里的候选，否则下完了还是找不到。
    """
    override = os.environ.get(MODEL_DIR_ENV, "").strip()
    if override:
        return Path(override)
    return app_data_dir() / "models"


def is_model_available() -> bool:
    """模型在不在本地。GUI 用它决定要不要弹下载引导。"""
    return find_model_snapshot() is not None


def _make_progress_tqdm(slot: "list[str]") -> type:
    """造一个 tqdm 子类：每步把百分比写进 slot[0]，供 portal 线程轮询。

    为什么用这种土办法：snapshot_download 跑在**工作线程**里，而推送日志的
    Channel 只能在 portal 线程上发。所以工作线程不直接发消息，只更新一个普通
    list 槽位，主线程每秒读一次再发 —— 线程安全，也不用加锁（单写单读 + 只存字符串）。
    """
    from tqdm.auto import tqdm as _tqdm

    class _ProgressTqdm(_tqdm):
        def update(self, n=1):  # type: ignore[override]
            result = super().update(n)
            total = float(self.total or 0)
            if total > 0:
                slot[0] = str(int(self.n * 100 / total))
            return result

    return _ProgressTqdm


def download_model(
    progress: "Callable[[str], None] | None" = None,
    percent_slot: Optional[list] = None,
) -> Path:
    """把嵌入模型下到 model_download_dir()，返回快照目录。

    progress 收一行人类可读的进度文本（GUI 日志面板用），可以传 None。
    percent_slot 是个单元素 list，下载过程中会被写入当前百分比字符串；
    调用方（GUI）轮询它来显示进度 —— 因为回调跑在工作线程里，不能直接发 Channel。
    幂等：已经有了就直接返回，不重复下载。
    """

    def say(msg: str) -> None:
        if progress is None:
            return
        try:
            progress(msg)
        except Exception:  # noqa: BLE001 - 日志失败不能拖垮下载
            pass

    existing = find_model_snapshot()
    if existing is not None:
        say("[模型] 本地已有，跳过下载：" + str(existing))
        return existing

    try:
        from huggingface_hub import snapshot_download
    except ImportError as e:  # pragma: no cover - 依赖缺失属于装坏了
        raise RuntimeError("缺少 huggingface_hub，无法下载嵌入模型") from e

    target = model_download_dir()
    target.mkdir(parents=True, exist_ok=True)
    say(f"[模型] 开始下载 {EMBED_MODEL_HF_REPO}（约 {EMBED_MODEL_APPROX_MB}MB）")
    say(f"[模型] 目标目录：{target}")

    slot = percent_slot if percent_slot is not None else [""]
    if slot:
        slot[0] = ""
    snapshot_download(
        repo_id=EMBED_MODEL_HF_REPO,
        revision=EMBED_MODEL_REVISION,
        local_dir=str(target),
        allow_patterns=EMBED_MODEL_ALLOW_PATTERNS,
        tqdm_class=_make_progress_tqdm(slot),
        max_workers=4,
    )

    found = find_model_snapshot()
    if found is None:
        raise RuntimeError(
            "模型下载完了，但在 " + str(target) + " 里找不到 config.json（可能被中断了，重试一次）"
        )
    say("[模型] 下载完成：" + str(found))
    return found


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
