from app.common.services.infrastructure import get_storage_bucket


class LazyStorageBucket:
    def __getattr__(self, item):
        return getattr(get_storage_bucket(), item)


bucket = LazyStorageBucket()
