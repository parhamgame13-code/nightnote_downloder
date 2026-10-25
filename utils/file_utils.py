import os
import shutil
from pathlib import Path

from loguru import logger


def get_file_size_mb(file_path: str) -> float:
    try:
        size = os.path.getsize(file_path)
        return round(size / (1024 * 1024), 2)
    except Exception:
        return 0.0


def cleanup_temp_dir(dir_path: str) -> None:
    try:
        if os.path.exists(dir_path):
            shutil.rmtree(dir_path, ignore_errors=True)
            logger.info(f"Cleaned up temp dir: {dir_path}")
    except Exception as e:
        logger.error(f"Failed to clean temp dir {dir_path}: {e}")


def ensure_temp_dir(base_dir: str, job_id: int) -> str:
    path = os.path.join(base_dir, str(job_id))
    os.makedirs(path, exist_ok=True)
    return path


def find_downloaded_file(dir_path: str) -> str | None:
    """اولین فایل داخل دایرکتوری رو برگردون."""
    try:
        for f in Path(dir_path).iterdir():
            if f.is_file() and not f.name.startswith('.'):
                return str(f)
    except Exception:
        pass
    return None


def format_size(size_mb: float) -> str:
    if size_mb >= 1024:
        return f"{size_mb / 1024:.1f} GB"
    return f"{size_mb:.1f} MB"
