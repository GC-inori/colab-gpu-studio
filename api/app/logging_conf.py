"""Shared process logging setup with optional gzip-compressed rotation."""

import gzip
import logging
import logging.handlers
import os
import shutil
from pathlib import Path

from .config import PROJECT_ROOT, sync_yaml_to_environment


class CompressedRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotate log files and gzip each archived generation."""

    def rotation_filename(self, default_name: str) -> str:
        return default_name + ".gz"

    def rotate(self, source: str, dest: str) -> None:
        if not os.path.exists(source):
            return
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        with open(source, "rb") as source_file, gzip.open(dest, "wb") as compressed:
            shutil.copyfileobj(source_file, compressed)
        os.remove(source)

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        if self.backupCount > 0:
            oldest = f"{self.baseFilename}.{self.backupCount}.gz"
            if os.path.exists(oldest):
                os.remove(oldest)
            for index in range(self.backupCount - 1, 0, -1):
                source = f"{self.baseFilename}.{index}.gz"
                destination = f"{self.baseFilename}.{index + 1}.gz"
                if os.path.exists(source):
                    os.replace(source, destination)
            self.rotate(self.baseFilename, self.rotation_filename(f"{self.baseFilename}.1"))
        elif os.path.exists(self.baseFilename):
            os.remove(self.baseFilename)
        if not self.delay:
            self.stream = self._open()


def configure_logging() -> None:
    config = sync_yaml_to_environment()
    options = config.get("logging") or {}
    if os.getenv("LOGGING_ENABLED", str(options.get("enabled", True))).strip().lower() in {"0", "false", "no", "off"}:
        return
    level = os.getenv("LOG_LEVEL", str(options.get("level", "INFO"))).upper()
    log_file = Path(os.getenv("LOG_FILE", str(options.get("file", "logs/studio.log"))))
    if not log_file.is_absolute():
        log_file = PROJECT_ROOT / log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler_type = CompressedRotatingFileHandler if os.getenv("LOG_COMPRESS", str(options.get("compress", True))).lower() not in {"0", "false", "no", "off"} else logging.handlers.RotatingFileHandler
    handler = handler_type(
        log_file,
        maxBytes=int(os.getenv("LOG_MAX_BYTES", options.get("max_bytes", 10 * 1024 * 1024))),
        backupCount=int(os.getenv("LOG_BACKUP_COUNT", options.get("backup_count", 5))),
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    if not root.handlers:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(console)
    # Idempotent for startup hooks and tests invoking it more than once.
    for previous in [h for h in root.handlers if getattr(h, "_studio_file_handler", False)]:
        root.removeHandler(previous)
        previous.close()
    handler._studio_file_handler = True
    root.addHandler(handler)
