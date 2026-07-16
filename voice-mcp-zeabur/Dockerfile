FROM node:18-alpine AS widget-builder
WORKDIR /build
COPY widget-src/ widget-src/
COPY scripts/ scripts/
RUN npm init -y && \
    npm install tsup @modelcontextprotocol/ext-apps && \
    npx tsup widget-src/voice-view-widget.ts --format iife --global-name VoiceWidget --out-dir dist/widget || \
    node scripts/build-widget.mjs

FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
COPY customize/ customize/
COPY templates/ templates/
COPY --from=widget-builder /build/dist/ dist/
CMD ["python3", "server.py"]
