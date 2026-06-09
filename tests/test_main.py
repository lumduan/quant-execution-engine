import runpy

import pytest
from src.main import main


@pytest.mark.asyncio
async def test_main_runs_without_error() -> None:
    await main()


def test_main_as_entrypoint() -> None:
    """Cover the __name__ == '__main__' path."""
    runpy.run_module("src.main", run_name="__main__")
