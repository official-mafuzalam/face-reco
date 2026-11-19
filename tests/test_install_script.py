import re
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parent.parent / "scripts" / "install.ps1").read_text(encoding="utf-8")


def test_ask_calls_pass_default_as_separate_argument() -> None:
    # `Ask "question", $false` passes one array argument: the prompt text breaks and the
    # default flips to yes, which makes -Unattended installs stop at a Read-Host.
    assert not re.search(r'\bAsk\s+"[^"]*"\s*,', SCRIPT)
