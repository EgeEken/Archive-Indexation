"""Float16 vector serialization and exact cosine helpers."""

from __future__ import annotations


def normalize_vector(vector):
    import numpy as np

    value = np.asarray(vector, dtype=np.float32).reshape(-1)
    if value.size == 0 or not np.isfinite(value).all():
        raise ValueError("embedding must be finite and non-empty")
    norm = float(np.linalg.norm(value))
    if norm <= 0:
        raise ValueError("embedding must have non-zero norm")
    return value / norm


def vector_to_blob(vector) -> tuple[bytes, int]:
    normalized = normalize_vector(vector)
    return normalized.astype("float16").tobytes(), int(normalized.size)


def blob_to_vector(blob: bytes, dimension: int):
    import numpy as np

    value = np.frombuffer(blob, dtype=np.float16)
    if value.size != dimension:
        raise ValueError(f"embedding dimension mismatch: expected {dimension}, got {value.size}")
    value = value.astype(np.float32)
    if not np.isfinite(value).all() or float(np.linalg.norm(value)) <= 0:
        raise ValueError("stored embedding is invalid")
    return value / np.linalg.norm(value)


def exact_top_k(matrix, query, top_k: int):
    import numpy as np

    if top_k < 1:
        return []
    query_vector = normalize_vector(query)
    scores = np.asarray(matrix, dtype=np.float32) @ query_vector
    count = min(top_k, scores.size)
    if count == 0:
        return []
    candidates = np.argpartition(-scores, count - 1)[:count]
    return sorted(((int(index), float(scores[index])) for index in candidates), key=lambda pair: (-pair[1], pair[0]))
