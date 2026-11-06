import os
import shutil

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def kustomize():
    """Real kustomize. Skipped locally when missing; CI sets CI=true and installs it, so it fails there."""
    path = shutil.which("kustomize")
    if path is None:
        if os.environ.get("CI"):
            pytest.fail("kustomize is required in CI")
        pytest.skip("kustomize not installed")
    return path
