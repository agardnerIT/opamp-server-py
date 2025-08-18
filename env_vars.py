import os

SERVER_HTTP_SCHEME = os.environ.get("SERVER_HTTP_SCHEME", "http")
SERVER_ADDR = os.environ.get("SERVER_ADDRESS", "localhost")
SERVER_PORT = os.environ.get("SERVER_PORT", "4320")