from app.common.services.infrastructure import get_firestore_client


class LazyFirestoreClient:
    def __getattr__(self, item):
        return getattr(get_firestore_client(), item)


db = LazyFirestoreClient()
