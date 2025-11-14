# ----------------------------------------------------
# Stage 1: Build Dependencies
# Use a slim image for a minimal Linux environment (Debian-based)
# This provides better glibc compatibility than Alpine for many Python libraries.
# ----------------------------------------------------
FROM python:3.11-slim AS build

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Set the working directory
WORKDIR /app

# Copy requirements.txt first to leverage Docker's layer caching
COPY requirements.txt .

# Install dependencies
# --no-cache-dir: Reduces image size by preventing pip from caching packages.
# NOTE: Add your production ASGI server (e.g., gunicorn) to requirements.txt
RUN pip install --no-cache-dir -r requirements.txt


# ----------------------------------------------------
# Stage 2: Final Runtime Image (Highly Minimal and Secure)
# ----------------------------------------------------
FROM python:3.11-slim

# Security Best Practice: Create and switch to a dedicated non-root user (Principle of Least Privilege).
# This prevents potential container breakout exploits from gaining root access.
RUN adduser --system --uid 1001 --group appuser

# Set working directory and required port
WORKDIR /app
ENV PORT=8000

# Copy *only* the installed packages and application code from the build stage.
# This leaves behind build tools, cache, and other unnecessary files.
COPY --from=build /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY server.py .
# Copy any other required static/template files if your app needs them
# COPY templates templates/

# Switch to the non-root user for the running container process
USER appuser

# Expose the application port
EXPOSE ${PORT}

# Final run command (Exec form is best for K8s signal handling)
# Use a production-ready server like gunicorn with uvicorn workers for concurrency.
# Assuming your app object is named 'app' in the 'server.py' file:
CMD ["gunicorn", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "server:app", "--bind", "0.0.0.0:8000"]