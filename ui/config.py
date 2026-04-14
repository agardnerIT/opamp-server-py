import os
import streamlit as st

SERVER_HTTP_SCHEME = os.environ.get("SERVER_HTTP_SCHEME", "http")
SERVER_ADDRESS = os.environ.get("SERVER_ADDRESS", "localhost")
SERVER_PORT = os.environ.get("SERVER_PORT", "4320")
SERVER_URL = f"{SERVER_HTTP_SCHEME}://{SERVER_ADDRESS}:{SERVER_PORT}"
