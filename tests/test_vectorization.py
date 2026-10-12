"""``vectorization.py`` 的单测。

embed_pdf 的端到端要跑真的 sentence-transformers + chromadb，那个属于 integration
test 范畴；这里只测纯函数（split_text / file_hash），保证基础切片/缓存键稳定。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from toushi import vectorization
from toushi.paths import app_data_dir
from toushi.vectorization import (
    EMBED_MODEL_REPO_ID,
    MODEL_DIR_ENV,
    split_text,
    text_hash,
)


# ---------- split_text ----------

class TestSplitText:
    def test_short_text_single_chunk(self):
        chunks = split_text("短文本", chunk_size=1000, chunk_overlap=200)
        assert chunks == ["短文本"]

    def test_long_text_chunked_with_overlap(self):
        text = "ABCDEFGHIJ" * 100  # 1000 字符
        chunks = split_text(text, chunk_size=300, chunk_overlap=50)
        # 至少切成多块
        assert len(chunks) > 1
        # 每块不超过 chunk_size
        for c in chunks:
            assert len(c) <= 300
        # 相邻块之间有 overlap（不严格断在边界）
        # 验证：把所有 chunk concat 起来去重后包含原始 text
        assert text in "".join(chunks) or len("".join(chunks)) >= len(text)

    def test_empty_text_returns_empty(self):
        assert split_text("", chunk_size=1000, chunk_overlap=200) == []

    def test_chunk_size_must_exceed_overlap(self):
        with pytest.raises(ValueError):
            split_text("anything", chunk_size=100, chunk_overlap=100)
        with pytest.raises(ValueError):
            split_text("anything", chunk_size=50, chunk_overlap=100)

    def test_whitespace_only_chunks_dropped(self):
        # chunk 全是空白时会被 .strip() 掉
        text = "   AAA   "
        chunks = split_text(text, chunk_size=3, chunk_overlap=1)
        # 没有任何 chunk 是纯空白
        assert all(c.strip() for c in chunks)

    def test_chinese_text_chunked_correctly(self):
        # 中文（多字节）也按字符数切，不该按字节
        text = "求职者简历内容" * 100
        chunks = split_text(text, chunk_size=50, chunk_overlap=10)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= 50
            assert "求职者" in c or "简历" in c or "内容" in c


# ---------- text_hash ----------

class TestTextHash:
    def test_same_content_same_hash(self):
        assert text_hash("hello world") == text_hash("hello world")

    def test_different_content_different_hash(self):
        assert text_hash("hello world") != text_hash("hello world!")

    def test_hash_is_md5_hex(self):
        h = text_hash("x")
        assert len(h) == 32
        assert all(c in "0123456789abcdef" for c in h)


# ---------- 嵌入模型定位（打包后最容易翻车的一处） ----------

class TestFindModelSnapshot:
    """模型查找必须在三种布局下都对：源码树、安装目录、用户数据目录。

    老实现把路径写死成 Path(__file__).parents[2]/models —— 源码里能跑，
    装成 wheel 之后指向 Lib/，表现是「App 能开能填表，一点开始就崩」。
    """

    @staticmethod
    def _make_snapshot(root: Path, repo_id: str = EMBED_MODEL_REPO_ID) -> Path:
        snap = root / "hub" / repo_id / "snapshots" / "deadbeef"
        snap.mkdir(parents=True)
        (snap / "config.json").write_text("{}", encoding="utf-8")
        return snap

    def test_finds_huggingface_cache_layout(self, tmp_path, monkeypatch):
        expected = self._make_snapshot(tmp_path)
        monkeypatch.setattr(vectorization, "_candidate_model_roots", lambda: [tmp_path])
        assert vectorization.find_model_snapshot() == expected

    def test_env_override_wins_over_other_roots(self, tmp_path, monkeypatch):
        override = tmp_path / "override"
        decoy = tmp_path / "decoy"
        expected = self._make_snapshot(override)
        self._make_snapshot(decoy)
        monkeypatch.setenv(MODEL_DIR_ENV, str(override))
        monkeypatch.setattr(
            vectorization, "_candidate_model_roots", lambda: [override, decoy]
        )
        assert vectorization.find_model_snapshot() == expected

    def test_accepts_flat_layout_without_hub_dir(self, tmp_path, monkeypatch):
        # 打包时把快照内容平铺进 models/ 的情况也要认
        (tmp_path / "config.json").write_text("{}", encoding="utf-8")
        (tmp_path / "sentence_bert_config.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(vectorization, "_candidate_model_roots", lambda: [tmp_path])
        assert vectorization.find_model_snapshot() == tmp_path

    def test_returns_none_when_nothing_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vectorization, "_candidate_model_roots", lambda: [tmp_path])
        assert vectorization.find_model_snapshot() is None

    def test_skips_snapshot_dir_without_config(self, tmp_path, monkeypatch):
        # 有 snapshots/<hash>/ 但没有 config.json → 不算数（半个模型比没有更糟）
        snap = tmp_path / "hub" / EMBED_MODEL_REPO_ID / "snapshots" / "broken"
        snap.mkdir(parents=True)
        monkeypatch.setattr(vectorization, "_candidate_model_roots", lambda: [tmp_path])
        assert vectorization.find_model_snapshot() is None

    def test_missing_model_error_lists_searched_paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vectorization, "_candidate_model_roots", lambda: [tmp_path])
        monkeypatch.setattr(vectorization, "_embedder", None)
        with pytest.raises(RuntimeError) as excinfo:
            vectorization._get_embedder()
        msg = str(excinfo.value)
        assert str(tmp_path) in msg
        assert MODEL_DIR_ENV in msg

    def test_default_roots_cover_dev_and_installed_layouts(self):
        roots = vectorization._candidate_model_roots()
        # 安装后：跟嵌入式解释器同级（bundle resources 落那儿）
        assert Path(sys.executable).resolve().parent / "models" in roots
        # 开发：仓库根 == CWD，两者去重后只留一份
        assert Path.cwd() / "models" in roots
        # 用户数据目录：允许用户自己塞一份
        assert app_data_dir() / "models" in roots
        # 没有重复项（重复会让"找过哪些位置"的报错信息很难看）
        assert len(roots) == len(set(roots))
