from collections.abc import Sequence

import numpy as np


class Gallery:
    def __init__(
        self,
        person_ids: Sequence[int | str],
        embeddings: np.ndarray | Sequence[Sequence[float]],
        threshold: float = 0.363,
        margin: float = 0.05,
    ) -> None:
        matrix = np.asarray(embeddings, dtype=np.float32)
        if matrix.size == 0:
            matrix = np.empty((0, 128), dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[1] != 128:
            raise ValueError("Gallery embeddings must have shape (n, 128).")
        if len(person_ids) != matrix.shape[0]:
            raise ValueError("Each gallery embedding must have a person id.")
        if threshold < -1.0 or threshold > 1.0:
            raise ValueError("threshold must be between -1 and 1.")
        if margin < 0:
            raise ValueError("margin must be non-negative.")

        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        if np.any(~np.isfinite(matrix)) or np.any(norms == 0):
            raise ValueError("Gallery embeddings must be finite and non-zero.")
        self.person_ids = tuple(person_ids)
        self.embeddings = (matrix / norms).astype(np.float32)
        self.threshold = threshold
        self.margin = margin

    def match(self, emb: np.ndarray) -> tuple[int | str | None, float, float]:
        query = np.asarray(emb, dtype=np.float32).reshape(-1)
        if query.shape != (128,):
            raise ValueError("Query embedding must contain 128 values.")
        norm = float(np.linalg.norm(query))
        if not np.isfinite(norm) or norm == 0:
            raise ValueError("Query embedding must be finite and non-zero.")
        if not self.person_ids:
            return None, 0.0, 0.0

        scores = self.embeddings @ (query / norm)
        best_index = int(np.argmax(scores))
        best_person = self.person_ids[best_index]
        best_score = float(scores[best_index])
        # The margin guards against confusing two different people, so the
        # runner-up must come from another person, not another sample of the same one.
        others = [float(score) for pid, score in zip(self.person_ids, scores) if pid != best_person]
        second_best = max(others) if others else 0.0
        if best_score >= self.threshold and best_score - second_best >= self.margin:
            return best_person, best_score, second_best
        return None, best_score, second_best
