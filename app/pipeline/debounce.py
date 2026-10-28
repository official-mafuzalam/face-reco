from dataclasses import dataclass
import threading

import numpy as np


@dataclass
class _UnknownGroup:
    embedding: np.ndarray
    last_logged: float


class EventDebouncer:
    def __init__(
        self,
        debounce_s: float = 45.0,
        unknown_debounce_s: float = 60.0,
        unknown_similarity: float = 0.75,
    ) -> None:
        if debounce_s < 0 or unknown_debounce_s < 0:
            raise ValueError("Debounce intervals must be non-negative.")
        if not -1 <= unknown_similarity <= 1:
            raise ValueError("unknown_similarity must be between -1 and 1.")
        self.debounce_s = debounce_s
        self.unknown_debounce_s = unknown_debounce_s
        self.unknown_similarity = unknown_similarity
        self._lock = threading.Lock()
        self._matched_at: dict[tuple[int, str, str], float] = {}
        self._unknown_groups: list[_UnknownGroup] = []

    def allow_matched(
        self,
        person_id: int,
        gate: str,
        direction: str,
        now: float,
    ) -> bool:
        key = (person_id, gate, direction)
        with self._lock:
            previous = self._matched_at.get(key)
            if previous is not None and now - previous < self.debounce_s:
                return False
            self._matched_at[key] = now
            return True

    def allow_unknown(self, embedding: np.ndarray, now: float) -> bool:
        vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vector))
        if vector.shape != (128,) or norm == 0 or not np.isfinite(norm):
            raise ValueError("Unknown embedding must be a finite non-zero 128-vector.")
        vector = vector / norm
        with self._lock:
            best_group: _UnknownGroup | None = None
            best_similarity = -1.0
            for group in self._unknown_groups:
                similarity = float(group.embedding @ vector)
                if similarity > best_similarity:
                    best_similarity = similarity
                    best_group = group
            if best_group is not None and best_similarity >= self.unknown_similarity:
                if now - best_group.last_logged < self.unknown_debounce_s:
                    return False
                best_group.embedding = vector.astype(np.float32)
                best_group.last_logged = now
                return True
            self._unknown_groups.append(_UnknownGroup(vector.astype(np.float32), now))
            return True
