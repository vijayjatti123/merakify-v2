"""Per-run, bounded image-byte cache; never shared across jobs or persisted."""
from collections import OrderedDict
from concurrent.futures import Future
from threading import Lock
from urllib.parse import urlsplit, urlunsplit, parse_qs

from app.config import settings


def source_identity(url):
    parsed = urlsplit(url)
    hosts = {f"{settings.aws_s3_bucket}.s3.amazonaws.com",
             f"{settings.aws_s3_bucket}.s3.{settings.aws_region}.amazonaws.com"}
    if parsed.scheme == "https" and parsed.hostname in hosts:
        # Signatures expire; the private object/version identity does not.
        version = parse_qs(parsed.query).get("versionId", [])
        return (urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")), tuple(version))
    return url  # External query strings may select different images.


class ReferenceCache:
    def __init__(self, max_bytes=128 * 1024 * 1024, max_items=64):
        self.max_bytes, self.max_items = max_bytes, max_items
        self.entries, self.lock = OrderedDict(), Lock()

    def _prune(self):
        completed = [(k, f) for k, f in self.entries.items() if f.done() and not f.exception()]
        size = sum(len(f.result().data) for _, f in completed)
        for key, future in completed:
            if size <= self.max_bytes and len(self.entries) <= self.max_items:
                break
            size -= len(future.result().data)
            self.entries.pop(key, None)

    def get(self, url, loader):
        key = source_identity(url)
        with self.lock:
            future = self.entries.get(key)
            owner = future is None
            if owner:
                future = Future()
                self.entries[key] = future
            self.entries.move_to_end(key)
        if owner:
            try:
                future.set_result(loader())
            except BaseException as error:
                future.set_exception(error)
                with self.lock:
                    self.entries.pop(key, None)
                raise
            with self.lock:
                self._prune()
        return future.result(), not owner

    def seed(self, url, image):
        future = Future()
        future.set_result(image)
        with self.lock:
            self.entries[source_identity(url)] = future
            self._prune()
