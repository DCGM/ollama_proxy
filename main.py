import uvicorn

from proxy.app import app
from proxy.config import config

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=config.proxy_host,
        port=config.proxy_port,
        log_level=config.log_level.lower(),
    )
