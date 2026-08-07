import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "network: hits live third-party APIs; deselected by default "
        "(run with -m network)")
