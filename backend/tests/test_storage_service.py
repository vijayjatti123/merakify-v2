import unittest
from io import BytesIO
from unittest.mock import patch

from app.config import settings
from app.services import storage_service


class FakeS3Client:
    def __init__(self) -> None:
        self.calls = []

    def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))
        return {"ETag": '"test-etag"'}

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        return {"Body": BytesIO(b"stored bytes")}

    def delete_object(self, **kwargs):
        self.calls.append(("delete_object", kwargs))
        return {}

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        self.calls.append(
            (
                "generate_presigned_url",
                {"operation": operation, "Params": Params, "ExpiresIn": ExpiresIn},
            )
        )
        return "https://s3.example.test/presigned"


class StorageServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = {
            "aws_s3_bucket": settings.aws_s3_bucket,
            "aws_region": settings.aws_region,
            "aws_access_key_id": settings.aws_access_key_id,
            "aws_secret_access_key": settings.aws_secret_access_key,
            "aws_s3_presigned_url_ttl_sec": settings.aws_s3_presigned_url_ttl_sec,
        }
        settings.aws_s3_bucket = "test-bucket"
        settings.aws_region = "ap-south-1"
        settings.aws_access_key_id = "test-access-key"
        settings.aws_secret_access_key = "test-secret-key"
        settings.aws_s3_presigned_url_ttl_sec = 900

    def tearDown(self) -> None:
        for name, value in self.original.items():
            setattr(settings, name, value)
        storage_service._s3_client.cache_clear()

    def test_private_s3_upload_download_delete_and_presigned_url(self) -> None:
        client = FakeS3Client()
        with patch.object(storage_service, "_s3_client", return_value=client):
            uploaded = storage_service.upload_bytes(
                "/tests/example file.txt",
                b"stored bytes",
                content_type="text/plain",
            )
            downloaded = storage_service.download_bytes(uploaded["key"])
            storage_service.delete_object(uploaded["key"])

        self.assertEqual(uploaded["bucket"], "test-bucket")
        self.assertEqual(uploaded["key"], "tests/example file.txt")
        self.assertEqual(uploaded["etag"], "test-etag")
        self.assertEqual(uploaded["url"], "https://s3.example.test/presigned")
        self.assertEqual(downloaded, b"stored bytes")
        self.assertEqual(
            [name for name, _ in client.calls],
            ["put_object", "generate_presigned_url", "get_object", "delete_object"],
        )
        self.assertEqual(client.calls[1][1]["ExpiresIn"], 900)

    def test_s3_client_uses_regional_sigv4_virtual_host_urls(self) -> None:
        with patch.object(storage_service.boto3, "client") as boto_client:
            storage_service._s3_client()

        kwargs = boto_client.call_args.kwargs
        self.assertEqual(kwargs["region_name"], "ap-south-1")
        self.assertEqual(kwargs["config"].signature_version, "s3v4")
        self.assertEqual(kwargs["config"].s3, {"addressing_style": "virtual"})

    def test_upload_file_streams_through_the_shared_upload_path(self) -> None:
        client = FakeS3Client()
        file = BytesIO(b"image bytes")
        with patch.object(storage_service, "_s3_client", return_value=client):
            uploaded = storage_service.upload_file("assets/example.png", file, content_type="image/png")

        self.assertEqual(uploaded["key"], "assets/example.png")
        self.assertIs(client.calls[0][1]["Body"], file)
        self.assertEqual(client.calls[0][1]["ContentType"], "image/png")

    def test_empty_object_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            storage_service.asset_url(" / ")


if __name__ == "__main__":
    unittest.main()
