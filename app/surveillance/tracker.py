from collections import deque
from dataclasses import dataclass, field

from app.pipeline.tracker import intersection_over_union
from app.surveillance.detector import ObjectDetection


@dataclass
class ObjectTrack:
    track_id: int
    category: str
    label: str
    box: tuple[float, float, float, float]
    score: float
    first_seen: float
    last_seen: float
    hits: int = 1
    # (time, centre x, centre y) samples, kept for later rules (loitering, line crossing, running)
    path: deque[tuple[float, float, float]] = field(default_factory=lambda: deque(maxlen=120))

    @property
    def center(self) -> tuple[float, float]:
        x, y, w, h = self.box
        return x + w / 2, y + h / 2

    @property
    def foot(self) -> tuple[float, float]:
        x, y, w, h = self.box
        return x + w / 2, y + h

    def is_stationary(self, now: float, window_s: float = 10.0) -> bool:
        """True when the object has been visible for `window_s` and barely moved in that time."""
        if now - self.first_seen < window_s:
            return False
        recent = [(x, y) for t, x, y in self.path if now - t <= window_s]
        if len(recent) < 2:
            return False
        limit = 0.5 * max(self.box[2], self.box[3])
        x0, y0 = recent[0]
        return all(abs(x - x0) <= limit and abs(y - y0) <= limit for x, y in recent)


class ObjectTracker:
    """Greedy IoU tracker per category. Tracks are confirmed after `min_hits` detections."""

    def __init__(self, iou_threshold: float = 0.25, lost_after_s: float = 3.0, min_hits: int = 2) -> None:
        self.iou_threshold = iou_threshold
        self.lost_after_s = lost_after_s
        self.min_hits = min_hits
        self.tracks: dict[int, ObjectTrack] = {}
        self._next_id = 1

    def update(self, detections: list[ObjectDetection], now: float) -> list[ObjectTrack]:
        pairs = sorted(
            (
                (intersection_over_union(track.box, det.box), track_id, index)
                for track_id, track in self.tracks.items()
                for index, det in enumerate(detections)
                if track.category == det.category
            ),
            reverse=True,
        )
        used_tracks: set[int] = set()
        used_dets: set[int] = set()
        for overlap, track_id, index in pairs:
            if overlap < self.iou_threshold:
                break
            if track_id in used_tracks or index in used_dets:
                continue
            used_tracks.add(track_id)
            used_dets.add(index)
            self._apply(self.tracks[track_id], detections[index], now)

        # Fallback for fast movers at low analysis fps: nearest centre within ~one box size.
        for distance, track_id, index in sorted(
            (
                self._centre_distance(track, det),
                track_id,
                index,
            )
            for track_id, track in self.tracks.items()
            if track_id not in used_tracks
            for index, det in enumerate(detections)
            if index not in used_dets and track.category == det.category
        ):
            if distance > 1.0:
                break
            if track_id in used_tracks or index in used_dets:
                continue
            used_tracks.add(track_id)
            used_dets.add(index)
            self._apply(self.tracks[track_id], detections[index], now)

        for index, det in enumerate(detections):
            if index in used_dets:
                continue
            track = ObjectTrack(self._next_id, det.category, det.label, det.box, det.score, now, now)
            track.path.append((now, *track.center))
            self.tracks[track.track_id] = track
            self._next_id += 1

        for track_id in [tid for tid, t in self.tracks.items() if now - t.last_seen > self.lost_after_s]:
            del self.tracks[track_id]
        return self.confirmed()

    def confirmed(self) -> list[ObjectTrack]:
        return [track for track in self.tracks.values() if track.hits >= self.min_hits]

    @staticmethod
    def _centre_distance(track: "ObjectTrack", det: ObjectDetection) -> float:
        """Centre distance in units of the track's larger box side."""
        tx, ty = track.center
        x, y, w, h = det.box
        scale = max(track.box[2], track.box[3], 1.0)
        return ((x + w / 2 - tx) ** 2 + (y + h / 2 - ty) ** 2) ** 0.5 / scale

    @staticmethod
    def _apply(track: ObjectTrack, det: ObjectDetection, now: float) -> None:
        track.box = det.box
        track.label = det.label
        track.score = det.score
        track.last_seen = now
        track.hits += 1
        track.path.append((now, *track.center))
