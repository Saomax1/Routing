"""python -m app  -> start the server (reads .env / environment)."""

import logging

import uvicorn

from .config import load_config
from .main import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    app = create_app(cfg)
    print(f"\n  Dispatch & Routing app  |  HCP mode: {cfg.hcp_mode}  |  geocoder: {cfg.geocoder}"
          f"\n  http://{cfg.host}:{cfg.port}\n", flush=True)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()
