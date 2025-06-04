import os
from firebase_admin import credentials, initialize_app, get_app
from dotenv import load_dotenv

load_dotenv()

def init_firebase():
    try:
        get_app()
    except ValueError:
        cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        cred = credentials.Certificate(cred_path)
        initialize_app(cred)