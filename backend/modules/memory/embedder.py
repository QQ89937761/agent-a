"""可插拔的嵌入器。

默认用 fastembed(本地 ONNX,支持中文,数据不出本机)。
留出接口以便测试时注入假嵌入器,也方便将来换别的本地模型。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class FastEmbedder:
    """基于 fastembed 的本地嵌入器。首次使用会下载模型(约几十 MB)。"""

    # 中文小模型,体积和常驻内存明显低于多语言模型。
    DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"

    def __init__(self, model_name: str | None = None):
        from fastembed import TextEmbedding  # 延迟导入,未装时上层会捕获

        try:
            from loguru import logger
            logger.disable("fastembed.common.model_management")
        except ImportError:
            pass
        cache_dir = Path(os.environ.get("XIAOA_HOME") or Path.cwd()) / "model-cache" / "fastembed"
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._model = TextEmbedding(
            model_name=model_name or self.DEFAULT_MODEL,
            cache_dir=str(cache_dir),
            threads=1,
        )
        # 探一次维度
        probe = list(self._model.embed(["_probe_"]))
        self.dim = len(probe[0])

    def embed(self, texts: list[str]) -> list[list[float]]:
        # 小批量避免 ONNX 在一次项目补索引时申请过大的临时张量。
        return [list(map(float, v)) for v in self._model.embed(texts, batch_size=8)]


def make_default_embedder(model_name: str | None = None) -> Embedder | None:
    """尝试构造本地嵌入器,失败返回 None(上层使用 SQLite 关键词检索)。"""
    try:
        return FastEmbedder(model_name=model_name)
    except Exception as e:  # 未装 fastembed / 下载失败
        print(f"[memory] 本地嵌入器不可用: {e}")
        return None
