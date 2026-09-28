import pytest


@pytest.fixture(autouse=True)
def _fresh_llm_usage():
    """La cuota de los modelos es global (se comparte entre cadenas): cada test empieza de cero."""
    from jarvis import llm

    llm._USAGE.clear()
    yield
    llm._USAGE.clear()
