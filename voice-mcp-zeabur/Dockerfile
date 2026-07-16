FROM node:18 AS widget-builder
WORKDIR /build
COPY widget-src/ widget-src/
RUN npm init -y && \
    npm install esbuild @modelcontextprotocol/ext-apps && \
    npx esbuild widget-src/voice-view-widget.ts \
      --bundle --format=iife --global-name=VoiceViewWidget \
      --platform=browser --target=es2020 --minify \
      --outfile=dist/widget/voice-view-widget.global.js

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
