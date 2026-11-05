"""Phase 3 real adapters (spec D3-6). Local models only; no network.

QwenVLM        image -> text (OCR and description), Qwen3.5-2B 8-bit via mlx-vlm
WhisperTranscriber  audio -> text, whisper large-v3-turbo q5_0 via whisper.cpp

Each adapter is a callable taking bytes and returning text, with a `model_id`.
Models load lazily on first use and stay resident for the adapter's lifetime.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

def _scratch_dir() -> str:
    """Scratch directory for adapter temp files: WEFT_SCRATCH_DIR, else the system temp dir.

    Resolved and created lazily at call time, never at import.
    """
    override = os.environ.get("WEFT_SCRATCH_DIR", "").strip()
    if not override:
        return tempfile.gettempdir()
    path = Path(override).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _image_suffix(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    raise ValueError("unsupported image format")


class QwenVLM:
    """OCR and description in one pass. Alpha OCR is accepted per the user (Oct 25)."""

    PROMPT = (
        "Transcribe all text visible in this image exactly as written. "
        "Then give a one-sentence description of the image."
    )

    def __init__(self, model_dir, prompt: str | None = None, max_tokens: int = 512):
        self.model_dir = Path(model_dir)
        self.prompt = prompt or self.PROMPT
        self.max_tokens = max_tokens
        self.model_id = "Qwen3.5-2B-8bit (mlx-vlm)"
        self._model = None
        self._processor = None
        self._config = None
        self._apply = None

    def _load(self):
        import json
        from mlx_vlm import load
        from mlx_vlm.prompt_utils import apply_chat_template
        self._model, self._processor = load(str(self.model_dir))
        self._config = json.loads((self.model_dir / "config.json").read_text())
        self._apply = apply_chat_template

    def __call__(self, data: bytes) -> str:
        if self._model is None:
            self._load()
        from mlx_vlm import generate
        fd, path = tempfile.mkstemp(suffix=_image_suffix(data), dir=_scratch_dir())
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            formatted = self._apply(self._processor, self._config, self.prompt, num_images=1)
            result = generate(self._model, self._processor, prompt=formatted,
                              image=path, verbose=False, max_tokens=self.max_tokens)
        finally:
            os.unlink(path)
        return (getattr(result, "text", None) or str(result)).strip()


class WhisperTranscriber:
    """Audio is converted to 16 kHz mono WAV with ffmpeg, then transcribed locally."""

    def __init__(self, model_path):
        self.model_path = Path(model_path)
        self.model_id = "whisper-large-v3-turbo-q5_0 (whisper.cpp)"
        self._model = None

    def _load(self):
        from pywhispercpp.model import Model
        self._model = Model(str(self.model_path))

    def _to_wav(self, data: bytes) -> str:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError("ffmpeg not found")
        fd_in, src = tempfile.mkstemp(suffix=".bin", dir=_scratch_dir())
        dst = None
        try:
            with os.fdopen(fd_in, "wb") as fh:
                fh.write(data)
            fd_out, dst = tempfile.mkstemp(suffix=".wav", dir=_scratch_dir())
            os.close(fd_out)
            subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", src,
                            "-ar", "16000", "-ac", "1", dst], check=True, timeout=600)
        except BaseException:
            if dst is not None:
                os.unlink(dst)
            raise
        finally:
            os.unlink(src)
        return dst

    def __call__(self, data: bytes) -> str:
        if self._model is None:
            self._load()
        wav = self._to_wav(data)
        try:
            segments = self._model.transcribe(wav)
            return " ".join(seg.text.strip() for seg in segments).strip()
        finally:
            os.unlink(wav)


class HarrierEmbedder:
    """Local text embeddings: microsoft/harrier-oss-v1-0.6b (MIT), 1024-dim, last-token pooling.

    Runs through mlx-embeddings (GPL-3.0; dependency licence recorded in the ADR). Queries
    take the model's retrieval instruction prefix; documents do not. Vectors are L2-normalised.
    """

    dim = 1024
    QUERY_PREFIX = (
        "Instruct: Given a web search query, retrieve relevant passages that answer the query\n"
        "Query: "
    )

    def __init__(self, model_dir):
        self.model_dir = Path(model_dir)
        self.model_id = "microsoft/harrier-oss-v1-0.6b (mlx-embeddings)"
        self._model = None
        self._tok = None

    def _load(self):
        from mlx_embeddings.utils import load
        self._model, self._tok = load(str(self.model_dir))

    def _embed(self, text: str) -> list:
        if self._model is None:
            self._load()
        import mlx.core as mx
        ids = self._tok.encode(text) if hasattr(self._tok, "encode") else self._tok.tokenizer.encode(text)
        out = self._model(mx.array([ids]))
        hidden = getattr(out, "last_hidden_state", None)
        if hidden is None:
            hidden = out.text_embeds if hasattr(out, "text_embeds") else out[0]
        vec = hidden[0, -1, :]
        norm = mx.sqrt(mx.sum(vec * vec)) + 1e-12
        return [float(x) for x in (vec / norm).tolist()]

    def embed_document(self, text: str) -> list:
        return self._embed(text)

    def embed_query(self, text: str) -> list:
        return self._embed(self.QUERY_PREFIX + text)


class Qwen3Embedder(HarrierEmbedder):
    """Qwen/Qwen3-Embedding-0.6B (Apache-2.0), 1024-dim, last-token pooling, Qwen3 query instruction.

    Same mlx-embeddings path as HarrierEmbedder; only the model and the query prefix differ.
    """

    QUERY_PREFIX = (
        "Instruct: Given a web search query, retrieve relevant passages that answer the query\n"
        "Query:"
    )

    def __init__(self, model_dir):
        super().__init__(model_dir)
        self.model_id = "Qwen/Qwen3-Embedding-0.6B (mlx-embeddings)"


class BgeM3Embedder(HarrierEmbedder):
    """BAAI/bge-m3 dense embeddings (MIT), 1024-dim, CLS pooling, no query instruction.

    Loaded from the mlx-community 4-bit build through mlx-embeddings (weights ~340 MB).
    """

    QUERY_PREFIX = ""

    def __init__(self, model_dir):
        super().__init__(model_dir)
        # Review ec112bc RESIDUAL-1: the id names the repo and revision, so different
        # checkpoints or quantisations never share a vector table.
        repo = re.search(r"models--([^/]+)", str(model_dir))
        rev = re.search(r"snapshots/([0-9a-f]+)", str(model_dir))
        name = repo.group(1).replace("--", "/") if repo else "local"
        self.model_id = f"BAAI/bge-m3 via {name}@{rev.group(1)[:12] if rev else 'unpinned'} (mlx-embeddings)"

    def _embed(self, text: str) -> list:
        if self._model is None:
            self._load()
        import mlx.core as mx
        ids = self._tok.encode(text) if hasattr(self._tok, "encode") else self._tok.tokenizer.encode(text)
        out = self._model(mx.array([ids]))
        vec = out.last_hidden_state[0, 0, :]  # CLS token
        norm = mx.sqrt(mx.sum(vec * vec)) + 1e-12
        return [float(x) for x in (vec / norm).tolist()]

    def embed_query(self, text: str) -> list:
        return self._embed(text)


def default_text_embedder():
    """Load and verify the adopted BAAI/bge-m3 4-bit MLX model from the local HF cache.

    Raises RuntimeError when the snapshot is absent, incomplete, or cannot produce a valid
    embedding. There is no silent cloud or lexical fallback.
    """
    import glob

    snapshots = sorted(glob.glob(os.path.expanduser(
        "~/.cache/huggingface/hub/models--mlx-community--bge-m3-mlx-4bit/snapshots/*")))
    if not snapshots:
        raise RuntimeError(
            "embedding model not present: expected mlx-community/bge-m3-mlx-4bit in the local "
            "Hugging Face cache")
    model_dir = Path(snapshots[-1])
    required = (model_dir / "config.json", model_dir / "tokenizer.json", model_dir / "model.safetensors")
    if any(not path.is_file() or path.stat().st_size == 0 for path in required):
        raise RuntimeError("embedding model unavailable: local BGE-M3 snapshot is incomplete")
    embedder = BgeM3Embedder(model_dir)
    try:
        probe = embedder.embed_query("Weft local model readiness check")
        if len(probe) != embedder.dim:
            raise ValueError(f"expected {embedder.dim} dimensions, got {len(probe)}")
    except Exception as exc:
        raise RuntimeError("embedding model unavailable: local BGE-M3 checkpoint failed to load") from exc
    return embedder
