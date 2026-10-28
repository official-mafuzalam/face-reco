from collections import Counter, deque
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class DetectionObservation:
    box: tuple[float, float, float, float]
    person_id: int | None
    score: float
    embedding: np.ndarray | None = None
    low_quality: bool = False


@dataclass
class Track:
    track_id: int
    box: tuple[float, float, float, float]
    last_frame: int
    history: deque[int | None]
    score_history: deque[float | None]
    low_quality_logged: bool = False
    matched_logged: int | None = None

    def consensus_person(self, required: int) -> int | None:
        counts = Counter(person_id for person_id in self.history if person_id is not None)
        if not counts:
            return None
        person_id, count = counts.most_common(1)[0]
        return person_id if count >= required else None

    def consensus_score(self, person_id: int) -> float | None:
        scores = [
            score
            for matched_id, score in zip(self.history, self.score_history, strict=True)
            if matched_id == person_id and score is not None
        ]
        return sum(scores) / len(scores) if scores else None


@dataclass(frozen=True)
class TrackedObservation:
    track: Track
    observation: DetectionObservation


def intersection_over_union(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    left = max(ax, bx)
    top = max(ay, by)
    right = min(ax + aw, bx + bw)
    bottom = min(ay + ah, by + bh)
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    union = aw * ah + bw * bh - intersection
    return intersection / union if union > 0 else 0.0


class IoUTracker:
    def __init__(
        self,
        consensus_n: int = 3,
        consensus_window: int = 5,
        iou_threshold: float = 0.3,
        max_missed_frames: int = 10,
    ) -> None:
        if consensus_n < 1 or consensus_window < consensus_n:
            raise ValueError("Consensus window must be at least consensus_n >= 1.")
        if not 0 < iou_threshold <= 1 or max_missed_frames < 0:
            raise ValueError("Invalid tracking thresholds.")
        self.consensus_n = consensus_n
        self.consensus_window = consensus_window
        self.iou_threshold = iou_threshold
        self.max_missed_frames = max_missed_frames
        self._tracks: dict[int, Track] = {}
        self._frame_index = 0
        self._next_track_id = 1

    def update(self, observations: list[DetectionObservation]) -> list[TrackedObservation]:
        self._frame_index += 1
        possible_pairs = sorted(
            (
                intersection_over_union(track.box, observation.box),
                track_id,
                observation_index,
            )
            for track_id, track in self._tracks.items()
            for observation_index, observation in enumerate(observations)
        )
        matched_tracks: set[int] = set()
        matched_observations: set[int] = set()
        assignments: dict[int, int] = {}
        for overlap, track_id, observation_index in reversed(possible_pairs):
            if overlap < self.iou_threshold:
                break
            if track_id in matched_tracks or observation_index in matched_observations:
                continue
            matched_tracks.add(track_id)
            matched_observations.add(observation_index)
            assignments[observation_index] = track_id

        tracked: list[TrackedObservation] = []
        for observation_index, observation in enumerate(observations):
            if observation_index not in assignments:
                track_id = self._next_track_id
                self._next_track_id += 1
                self._tracks[track_id] = Track(
                    track_id=track_id,
                    box=observation.box,
                    last_frame=self._frame_index,
                    history=deque(maxlen=self.consensus_window),
                    score_history=deque(maxlen=self.consensus_window),
                )
                assignments[observation_index] = track_id
            track = self._tracks[assignments[observation_index]]
            track.box = observation.box
            track.last_frame = self._frame_index
            track.history.append(None if observation.low_quality else observation.person_id)
            track.score_history.append(
                observation.score if observation.person_id is not None and not observation.low_quality else None
            )
            tracked.append(TrackedObservation(track, observation))

        stale = [
            track_id
            for track_id, track in self._tracks.items()
            if self._frame_index - track.last_frame > self.max_missed_frames
        ]
        for track_id in stale:
            del self._tracks[track_id]
        return tracked
