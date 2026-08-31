"""Shared helpers: project paths, config loading, logging, executable resolution."""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

import yaml

# Project root = parent of the bridge/ package.
ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | os.PathLike | None = None) -> dict:
    """Load config.yaml from the project root (or an explicit path)."""
    cfg_path = Path(path) if path else (ROOT / "config.yaml")
    with open(cfg_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def resolve(path_str: str) -> Path:
    """Resolve a config path relative to the project root (absolute stays put)."""
    p = Path(path_str)
    return p if p.is_absolute() else (ROOT / p)


def setup_logging(log_file: str, level: int = logging.INFO) -> logging.Logger:
    log_path = resolve(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("sts2advisor")
    logger.setLevel(level)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                            datefmt="%H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def resolve_executable(name: str) -> str:
    """Find the real path of a CLI executable (handles claude.cmd on Windows)."""
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        for ext in (".cmd", ".exe", ".bat"):
            found = shutil.which(name + ext)
            if found:
                return found
    return name  # fall back to bare name; subprocess will error clearly if missing
