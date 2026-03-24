from app.common.services.infrastructure import get_firebase_app


def init_firebase():
    return get_firebase_app()
