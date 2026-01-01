import os
from dotenv import load_dotenv

# Load environment variables from a .env file if it exists
load_dotenv()

class Config:
    """Base Configuration."""
    SECRET_KEY = os.environ.get('SECRET_KEY', 'default_dev_key')
    
    # Database Configuration
    MONGO_URI = os.environ.get('MONGO_URI', 'mongodb://localhost:27017/')
    DB_NAME = os.environ.get('DB_NAME', 'kitting_db')
    
    # --- IMAGE STORAGE CONFIG ---
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'captures')
    
    
    # S3 CONFIG (Placeholder for future)
    USE_S3 = False
    S3_BUCKET = "my-kitting-bucket"
    S3_REGION = "us-east-1"

    # Socket Server Configuration
    # This URL is passed to the frontend so JS knows where to connect
    # Use your actual IP or 0.0.0.0 so it listens on all interfaces
    SOCKET_SERVER_URL = os.environ.get('SOCKET_SERVER_URL', 'http://192.168.29.82:5000')

class DevelopmentConfig(Config):
    DEBUG = True

class ProductionConfig(Config):
    DEBUG = False

# Dictionary to map environment names to config objects
config_map = {
    'development': DevelopmentConfig,
    'production': ProductionConfig
}