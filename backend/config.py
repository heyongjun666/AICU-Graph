"""
Application configuration constants.
"""
import os

# Base directory for data CSV files
DATA_DIR = os.environ.get(
    "BLOG_DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
)

# API base path
API_PREFIX = "/api/v1"

# Default pagination
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 100

# Upload settings
UPLOAD_DIR = os.environ.get(
    "BLOG_UPLOAD_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads"),
)
UPLOAD_URL_PREFIX = "/uploads"

# CORS
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*")

# JWT
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-in-production")
