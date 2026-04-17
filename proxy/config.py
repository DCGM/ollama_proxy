import os


class Config:
    proxy_host: str = os.getenv("PROXY_HOST", "0.0.0.0")
    proxy_port: int = int(os.getenv("PROXY_PORT", "11434"))

    backend_host: str = os.getenv("BACKEND_HOST", "127.0.0.1")
    backend_port_start: int = int(os.getenv("BACKEND_PORT_START", "24000"))
    backend_port_end: int = int(os.getenv("BACKEND_PORT_END", "24200"))

    scan_interval_seconds: float = float(os.getenv("SCAN_INTERVAL_SECONDS", "5"))
    probe_timeout_seconds: float = float(os.getenv("PROBE_TIMEOUT_SECONDS", "1.0"))

    max_retries: int = int(os.getenv("MAX_RETRIES", "2"))
    max_concurrent_per_backend: int = int(os.getenv("MAX_CONCURRENT_PER_BACKEND", "1"))
    queue_timeout_seconds: float = float(os.getenv("QUEUE_TIMEOUT_SECONDS", "3600"))

    backend_connect_timeout_seconds: float = float(
        os.getenv("BACKEND_CONNECT_TIMEOUT_SECONDS", "2.0")
    )
    backend_read_timeout_seconds: float = float(
        os.getenv("BACKEND_READ_TIMEOUT_SECONDS", "80.0")
    )

    reject_streaming: bool = os.getenv("REJECT_STREAMING", "true").lower() == "true"
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    


config = Config()
