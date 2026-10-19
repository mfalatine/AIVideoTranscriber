"""The application's single version source."""
from pathlib import Path


VERSION = Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip()
