import pytest

pytest_plugins = ("pytest_asyncio",)


@pytest.fixture(autouse=True)
async def reset_server_state():
    from server import _reset_runtime_state

    await _reset_runtime_state()
    yield
    await _reset_runtime_state()
