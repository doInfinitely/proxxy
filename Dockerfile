# -- Stage 1: Build frontend --
FROM node:20-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# -- Stage 2: Backend + Chromium --
FROM python:3.13-slim

# Install Chromium, Xvfb (for headed mode), dumb-init, and deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    xvfb \
    xauth \
    dumb-init \
    fonts-liberation \
    libnss3 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libgbm1 \
    libasound2 \
    libxshmfence1 \
    libx11-xcb1 \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY backend/pyproject.toml /tmp/pyproject.toml
RUN pip install --no-cache-dir \
    "fastapi>=0.115.0" \
    "uvicorn[standard]>=0.34.0" \
    "websockets>=15.0" \
    "browser-use>=0.12.0" \
    "python-dotenv>=1.0"

# Tell browser-use to use the system Chromium
ENV CHROME_PATH=/usr/bin/chromium
ENV CHROMIUM_PATH=/usr/bin/chromium

# Maintain same directory structure as local dev
COPY backend/ /app/backend/
COPY --from=frontend-build /app/frontend/dist /app/frontend/dist

WORKDIR /app/backend

# Xvfb wrapper script for headed Chrome without a real display
RUN echo '#!/bin/bash\nXvfb :99 -screen 0 1920x1080x24 -nolisten tcp &\nsleep 1\nexport DISPLAY=:99\nexec "$@"' > /entrypoint.sh \
    && chmod +x /entrypoint.sh

ENV DISPLAY=:99

# Ensure /dev/shm is large enough (fallback — --disable-dev-shm-usage also set in code)
RUN mkdir -p /tmp/chrome-shm

EXPOSE 8000

ENTRYPOINT ["/usr/bin/dumb-init", "--", "/entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
