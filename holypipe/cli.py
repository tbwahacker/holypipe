"""Console-script entry point: `pip install holypipe && holypipe`."""
from __future__ import annotations

import os

import uvicorn

from .config import settings


def main() -> None:
    # Running under Docker's own CMD, or want to defer to your own uvicorn
    # invocation? Just skip this and run `uvicorn holypipe.main:app` directly.
    os.makedirs("data", exist_ok=True)
    uvicorn.run("holypipe.main:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
