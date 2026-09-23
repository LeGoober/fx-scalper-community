"""`python -m app` → run the API (127.0.0.1:5001 by default; see COMMUNITY_HOST / COMMUNITY_PORT)."""
import uvicorn

from app import config

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=config.host(), port=config.port(), log_level="info")
