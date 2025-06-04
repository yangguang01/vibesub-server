import os
from google.cloud import firestore
from dotenv import load_dotenv

load_dotenv()

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
db = firestore.Client(project=PROJECT_ID)