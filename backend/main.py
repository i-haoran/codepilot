import uvicorn
from app.core.logging import setup_logging

setup_logging()

from app.core.app import app  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(
        "app.core.app:app",
        host="0.0.0.0",
        port=8080,
        reload=True
    )
