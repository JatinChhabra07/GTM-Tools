import pytest


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Every test gets its own data/config/products dirs: never touch the real ones."""
    monkeypatch.setenv("GTM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GTM_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("GTM_PRODUCTS_DIR", str(tmp_path / "products"))
    # never create collections in the real Qdrant server; live tests opt back in explicitly
    monkeypatch.setenv("GTM_KB_BACKEND", "memory")
    return tmp_path
