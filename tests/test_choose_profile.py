from pathlib import Path

import pytest

from tools.choose_profile import notes, recommend_profile, set_env_value


@pytest.mark.parametrize("cameras, hardware, expected", [
    (3, {"cpu_cores": 6, "providers": ["CPUExecutionProvider"]}, "cpu_small"),
    (3, {"cpu_cores": 12, "providers": []}, "cpu_large"),
    (6, {"cpu_cores": 4, "providers": []}, "cpu_large"),
    (20, {"cpu_cores": 6, "providers": ["OpenVINOExecutionProvider"]}, "openvino"),
    (20, {"cpu_cores": 6, "providers": ["CUDAExecutionProvider"], "has_cuda": True}, "cuda"),
])
def test_profile_matches_cameras_and_usable_hardware(cameras, hardware, expected) -> None:
    assert recommend_profile(cameras, hardware) == expected


def test_gpu_without_the_gpu_package_is_explained_not_selected() -> None:
    hardware = {"cpu_cores": 6, "providers": ["CPUExecutionProvider"], "has_cuda": True}
    profile = recommend_profile(12, hardware)
    assert profile == "cpu_large"
    found = notes(12, hardware, profile)
    assert any("onnxruntime-gpu" in n for n in found) and any("12 cameras" in n for n in found)


def test_env_value_is_added_once_and_an_explicit_choice_is_kept(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("SECRET_KEY=abc\n# HARDWARE_PROFILE=cuda\n", encoding="utf-8")
    assert set_env_value(env, "HARDWARE_PROFILE", "cpu_large") is True  # a comment is not a choice
    assert set_env_value(env, "HARDWARE_PROFILE", "cuda") is False
    assert env.read_text(encoding="utf-8").splitlines() == ["SECRET_KEY=abc", "# HARDWARE_PROFILE=cuda", "HARDWARE_PROFILE=cpu_large"]
