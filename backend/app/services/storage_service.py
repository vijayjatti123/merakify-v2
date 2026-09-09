from functools import lru_cache
from typing import BinaryIO, Optional, Union

import boto3
from botocore.client import BaseClient

from app.config import settings


Body = Union[bytes, bytearray, memoryview, BinaryIO]


class StorageConfigurationError(RuntimeError):
    """Raised when the S3 service is used without complete configuration."""


def _required(value: str, env_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise StorageConfigurationError(f"{env_name} is required for object storage")
    return normalized


def _object_key(key: str) -> str:
    normalized = key.strip().lstrip("/")
    if not normalized:
        raise ValueError("S3 object key must not be empty")
    return normalized


def _bucket() -> str:
    return _required(settings.aws_s3_bucket, "AWS_S3_BUCKET")


@lru_cache(maxsize=1)
def _s3_client() -> BaseClient:
    region = _required(settings.aws_region, "AWS_REGION")
    access_key = _required(settings.aws_access_key_id, "AWS_ACCESS_KEY_ID")
    secret_key = _required(settings.aws_secret_access_key, "AWS_SECRET_ACCESS_KEY")
    return boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )


def asset_url(key: str, expires_in: Optional[int] = None) -> str:
    """Return a time-limited URL for an object in the private S3 bucket."""
    normalized_key = _object_key(key)
    ttl = expires_in if expires_in is not None else settings.aws_s3_presigned_url_ttl_sec
    if ttl <= 0:
        raise ValueError("Presigned URL expiry must be greater than zero")
    return _s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": _bucket(), "Key": normalized_key},
        ExpiresIn=ttl,
    )


def upload_bytes(
    key: str,
    body: Body,
    *,
    content_type: Optional[str] = None,
    cache_control: Optional[str] = None,
) -> dict:
    """Upload an object and return its stable identity plus its current access URL."""
    normalized_key = _object_key(key)
    request = {"Bucket": _bucket(), "Key": normalized_key, "Body": body}
    if content_type:
        request["ContentType"] = content_type
    if cache_control:
        request["CacheControl"] = cache_control

    response = _s3_client().put_object(**request)
    return {
        "bucket": _bucket(),
        "key": normalized_key,
        "etag": response.get("ETag", "").strip('"'),
        "url": asset_url(normalized_key),
    }


def download_bytes(key: str) -> bytes:
    """Read an object from the private bucket."""
    response = _s3_client().get_object(Bucket=_bucket(), Key=_object_key(key))
    return response["Body"].read()


def delete_object(key: str) -> None:
    """Delete an object from the private bucket."""
    _s3_client().delete_object(Bucket=_bucket(), Key=_object_key(key))
