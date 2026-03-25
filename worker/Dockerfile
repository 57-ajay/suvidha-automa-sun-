FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 libxshmfence1 \
    libx11-6 libx11-xcb1 libxcb1 libxext6 libxfixes3 \
    libglib2.0-0 libdbus-1-3 libexpat1 \
    fonts-liberation fonts-noto-color-emoji \
    xvfb x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/*

RUN pip install uv

WORKDIR /app
COPY pyproject.toml ./
RUN uv sync

ENV PATH="/app/.venv/bin:$PATH"
RUN python -m patchright install chromium

RUN apt-get update && apt-get install -y --no-install-recommends \
    iproute2 curl \
    && rm -rf /var/lib/apt/lists/*

COPY . .
RUN chmod +x start.sh

CMD ["./start.sh"]
