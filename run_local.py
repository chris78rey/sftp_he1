from __future__ import annotations

from app import app
from config import Settings


if __name__ == "__main__":
    app.run(host=Settings.APP_HOST, port=Settings.APP_PORT, debug=False)
