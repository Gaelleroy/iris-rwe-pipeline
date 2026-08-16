"""Config loading and the run manifest.

The manifest is the reproducibility artifact. Four things get pinned to every
result: the code (git SHA), the config (content hash), the data (row counts and
input versions), and the environment (interpreter + package versions). If any
one of those is missing you cannot honestly claim a result is reproducible.
"""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

CONFIG_DEFAULT = Path(__file__).resolve().parents[1] / "config" / "study.yaml"


def load_config(path: str | Path | None = None) -> dict:
    path = Path(path or CONFIG_DEFAULT)
    with open(path) as fh:
        cfg = yaml.safe_load(fh)
    cfg["_config_path"] = str(path)
    cfg["_config_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return cfg


def git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=Path(__file__).resolve().parents[1],
        )
        sha = out.stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
            cwd=Path(__file__).resolve().parents[1],
        ).stdout.strip()
        if not sha:
            return "not-a-git-repo"
        return f"{sha}{'-dirty' if dirty else ''}"
    except Exception:
        return "unknown"


def environment() -> dict:
    pkgs = {}
    for name in ("pandas", "numpy", "pyarrow", "duckdb", "yaml"):
        try:
            mod = __import__(name)
            pkgs[name] = getattr(mod, "__version__", "unknown")
        except ImportError:
            pkgs[name] = "not-installed"
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "packages": pkgs,
    }


class RunManifest:
    """Accumulates provenance across pipeline stages, then writes it once."""

    def __init__(self, cfg: dict, run_id: str | None = None):
        self.run_id = run_id or datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ-"
        ) + uuid.uuid4().hex[:6]
        self.cfg = cfg
        self.doc: dict[str, Any] = {
            "run_id": self.run_id,
            "started_utc": datetime.now(timezone.utc).isoformat(),
            "study": {
                "id": cfg["study"]["id"],
                "version": cfg["study"]["version"],
            },
            "pins": {
                "code_git_sha": git_sha(),
                "config_path": cfg.get("_config_path"),
                "config_sha256": cfg.get("_config_sha256"),
                "storage_backend": cfg["storage"]["backend"],
                "query_engine": cfg["query"]["engine"],
            },
            "environment": environment(),
            "stages": [],
        }

    def record(self, stage: str, **detail) -> None:
        entry = {
            "stage": stage,
            "completed_utc": datetime.now(timezone.utc).isoformat(),
            **detail,
        }
        self.doc["stages"].append(entry)

    def write(self, storage, layer: str = "results") -> str:
        self.doc["finished_utc"] = datetime.now(timezone.utc).isoformat()
        key = f"{layer}/{self.run_id}/run_manifest.json"
        payload = json.dumps(self.doc, indent=2, default=str).encode()
        storage.write_bytes(key, payload)
        # also write a stable pointer to the latest run
        storage.write_bytes(
            f"{layer}/latest_run.txt", self.run_id.encode()
        )
        return storage.uri(key)

    def __repr__(self) -> str:
        return f"<RunManifest {self.run_id} stages={len(self.doc['stages'])}>"
