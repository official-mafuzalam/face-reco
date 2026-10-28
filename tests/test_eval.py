import numpy as np

from tools.eval_accuracy import ImageSample, evaluate_samples


class FakeDetector:
    def detect(self, _image: np.ndarray) -> list[object]:
        return [object()]


class FakeEmbedder:
    def align_and_embed(self, image: np.ndarray, _face: object) -> np.ndarray:
        return image.reshape(-1)[:128].astype(np.float32)


class FakeQuality:
    def check(self, _image: np.ndarray, _face: object) -> tuple[bool, list[str]]:
        return True, []


def _image(index: int) -> np.ndarray:
    vector = np.zeros(128, dtype=np.float32)
    vector[index] = 1.0
    return vector.reshape(1, 1, 128)


def test_eval_known_gallery_and_probe_metrics() -> None:
    samples = {
        "A": [
            ImageSample("A", "a-01.jpg", _image(0)),
            ImageSample("A", "a-02.jpg", _image(0)),
        ],
        "B": [
            ImageSample("B", "b-01.jpg", _image(1)),
            ImageSample("B", "b-02.jpg", _image(1)),
        ],
        "_unknown": [ImageSample("_unknown", "unknown-01.jpg", _image(2))],
    }

    result = evaluate_samples(
        samples,
        FakeDetector(),
        FakeEmbedder(),
        FakeQuality(),
        k=1,
        thresholds=(0.2, 0.363),
    )

    assert result["gallery_count"] == 2
    assert result["known_probe_count"] == 2
    assert result["unknown_probe_count"] == 1
    assert result["dropped"] == {}
    assert result["default_top1_accuracy"] == 1.0
    for row in result["threshold_rows"]:
        assert row["true_accept_rate"] == 1.0
        assert row["false_reject_rate"] == 0.0
        assert row["wrong_person_rate"] == 0.0
        assert row["false_accept_rate"] == 0.0


def test_eval_counts_quality_and_face_rejection_reasons() -> None:
    class MixedDetector:
        def detect(self, image: np.ndarray) -> list[object]:
            if image[0, 0, 0] == 1:
                return []
            if image[0, 0, 0] == 2:
                return [object(), object()]
            return [object()]

    class RejectOne(FakeQuality):
        def check(self, image: np.ndarray, _face: object) -> tuple[bool, list[str]]:
            return (False, ["blurred"]) if image[0, 0, 0] == 3 else (True, [])

    class MarkerEmbedder:
        def align_and_embed(self, image: np.ndarray, _face: object) -> np.ndarray:
            embedding = np.zeros(128, dtype=np.float32)
            embedding[int(image[0, 0, 0])] = 1.0
            return embedding

    samples = {
        "A": [
            ImageSample("A", "01.jpg", _image(0)),
            ImageSample("A", "02.jpg", _image(1)),
            ImageSample("A", "03.jpg", _image(2)),
            ImageSample("A", "04.jpg", _image(3)),
        ]
    }
    # Encode small marker values for the fake detector and quality checker.
    for index, sample in enumerate(samples["A"]):
        sample.image[0, 0, 0] = index

    result = evaluate_samples(
        samples,
        MixedDetector(),
        MarkerEmbedder(),
        RejectOne(),
        k=1,
        thresholds=(0.363,),
    )

    assert result["dropped"] == {"blurred": 1, "multiple_faces": 1, "no_face": 1}
    assert result["gallery_count"] == 1
    assert result["known_probe_count"] == 0
