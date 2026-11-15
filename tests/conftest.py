import os

import pytest

# Tests run with the built-in development keys; production refuses them.
os.environ.setdefault("ALLOW_INSECURE_DEFAULTS", "true")
# Unit tests drive the surveillance pieces directly; don't spawn real workers from routes.
os.environ.setdefault("SURVEILLANCE_ENABLED", "false")


@pytest.fixture(autouse=True)
def _reset_login_limiter():
    from app.security import login_limiter

    login_limiter._failures.clear()
    yield
    login_limiter._failures.clear()
