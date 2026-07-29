FROM python:3.12-slim

# parivahan's Angular serializes datetime-local values ("YYYY-MM-DDTHH:MM",
# no timezone) through the BROWSER's local timezone. python:3.12-slim
# defaults to UTC, which shifted every HR/PB/HP tax window by +05:30 on the
# paid receipt (requested 15:44 IST -> stored 15:44 UTC -> printed 21:14
# IST). Chromium inherits TZ from the container env, so pin the whole
# container to IST. scripted/steps.py:force_ist_timezone is the per-session
# CDP backstop for this.
ENV TZ=Asia/Kolkata

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    tzdata \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 libxshmfence1 \
    libx11-6 libx11-xcb1 libxcb1 libxext6 libxfixes3 \
    libglib2.0-0 libdbus-1-3 libexpat1 \
    fonts-liberation fonts-noto-color-emoji \
    xvfb x11vnc novnc websockify \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
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
