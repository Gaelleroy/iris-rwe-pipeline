"""Storage abstraction.

One interface, two backends. Everything downstream writes through this, which
is what makes `backend: local` -> `backend: s3` a config change rather than a
rewrite. Layer names (raw/curated/analytics/results) are fixed vocabulary.

The local backend uses the same directory layout and the same Hive-style
partition paths that S3 uses, so a path that works locally works on S3.
"""
from __future__ import annotations

import io
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable

import pandas as pd

LAYERS = ("raw", "curated", "analytics", "results", "metadata")


class StorageBackend(ABC):
    """Minimal interface every backend must satisfy."""

    @abstractmethod
    def uri(self, key: str) -> str:
        """Fully-qualified location for a key, for logging and manifests."""

    @abstractmethod
    def write_bytes(self, key: str, data: bytes) -> str: ...

    @abstractmethod
    def read_bytes(self, key: str) -> bytes: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def list(self, prefix: str) -> list[str]: ...

    # -- convenience layers on top of the byte interface --------------------

    def write_csv(self, df: pd.DataFrame, key: str) -> str:
        buf = io.StringIO()
        df.to_csv(buf, index=False)
        return self.write_bytes(key, buf.getvalue().encode("utf-8"))

    def read_csv(self, key: str, **kwargs) -> pd.DataFrame:
        return pd.read_csv(io.BytesIO(self.read_bytes(key)), **kwargs)

    def write_parquet(
        self, df: pd.DataFrame, key: str, partition_by: Iterable[str] | None = None
    ) -> str:
        """Write parquet, optionally Hive-partitioned.

        Partitioning matters more than it looks: on Athena you are billed by
        bytes scanned, so `WHERE site_id = 'SITE_03'` on a partitioned table
        reads one prefix instead of the whole dataset.
        """
        partition_by = list(partition_by or [])
        if not partition_by:
            buf = io.BytesIO()
            df.to_parquet(buf, index=False)
            return self.write_bytes(key.rstrip("/") + "/part-0000.parquet", buf.getvalue())

        written = []
        for values, chunk in df.groupby(partition_by, dropna=False, observed=True):
            values = values if isinstance(values, tuple) else (values,)
            sub = "/".join(f"{col}={val}" for col, val in zip(partition_by, values))
            part_key = f"{key.rstrip('/')}/{sub}/part-0000.parquet"
            buf = io.BytesIO()
            chunk.drop(columns=partition_by).to_parquet(buf, index=False)
            written.append(self.write_bytes(part_key, buf.getvalue()))
        return self.uri(key)

    def read_parquet(self, key: str) -> pd.DataFrame:
        """Read a dataset, walking partition directories if present."""
        keys = [k for k in self.list(key) if k.endswith(".parquet")]
        if not keys:
            raise FileNotFoundError(f"no parquet under {self.uri(key)}")
        frames = []
        for k in keys:
            frame = pd.read_parquet(io.BytesIO(self.read_bytes(k)))
            # recover Hive partition columns from the path
            rel = k[len(key):].strip("/")
            for segment in rel.split("/")[:-1]:
                if "=" in segment:
                    col, val = segment.split("=", 1)
                    frame[col] = val
            frames.append(frame)
        return pd.concat(frames, ignore_index=True)


class LocalBackend(StorageBackend):
    def __init__(self, root: str = "./data"):
        self.root = Path(root).resolve()
        for layer in LAYERS:
            (self.root / layer).mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / key.lstrip("/")

    def uri(self, key: str) -> str:
        return str(self._path(key))

    def write_bytes(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def read_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def list(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        if base.is_file():
            return [prefix]
        if not base.exists():
            return []
        out = []
        for p in sorted(base.rglob("*")):
            if p.is_file():
                out.append(str(p.relative_to(self.root)))
        return out


class S3Backend(StorageBackend):
    """S3 backend. Requires boto3 and credentials; not exercised in CI.

    Deliberately thin - the point is that nothing above this class knows or
    cares which backend it is talking to.
    """

    def __init__(self, bucket: str, prefix: str = "", region: str = "us-east-1"):
        try:
            import boto3  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise ImportError("S3 backend requires boto3: pip install boto3") from exc
        import boto3

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.client = boto3.client("s3", region_name=region)

    def _key(self, key: str) -> str:
        key = key.lstrip("/")
        return f"{self.prefix}/{key}" if self.prefix else key

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{self._key(key)}"

    def write_bytes(self, key: str, data: bytes) -> str:  # pragma: no cover
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)
        return self.uri(key)

    def read_bytes(self, key: str) -> bytes:  # pragma: no cover
        obj = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        return obj["Body"].read()

    def exists(self, key: str) -> bool:  # pragma: no cover
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except ClientError:
            return bool(self.list(key))

    def list(self, prefix: str) -> list[str]:  # pragma: no cover
        paginator = self.client.get_paginator("list_objects_v2")
        full = self._key(prefix)
        out = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full):
            for item in page.get("Contents", []):
                k = item["Key"]
                out.append(k[len(self.prefix) + 1:] if self.prefix else k)
        return sorted(out)


def get_backend(cfg: dict) -> StorageBackend:
    """Factory. The only place in the codebase that branches on backend."""
    scfg = cfg["storage"]
    kind = scfg.get("backend", "local")
    if kind == "local":
        return LocalBackend(**scfg["local"])
    if kind == "s3":
        return S3Backend(**scfg["s3"])
    raise ValueError(f"unknown storage backend: {kind}")
