import re
from pathlib import Path

import pytest

from mixsync.core.config import Settings

ENV_EXAMPLE = Path(__file__).parents[3] / "deploy" / ".env.example"


@pytest.mark.parametrize("name", sorted(Settings.model_fields))
def test_env_example_lists_every_setting(name: str) -> None:
    var = f"MIXSYNC_{name.upper()}"
    # Whole-name match, so MIXSYNC_FOO_BAR does not satisfy MIXSYNC_FOO.
    assert re.search(rf"\b{var}\b", ENV_EXAMPLE.read_text()), (
        f"{var} is missing from deploy/.env.example"
    )
