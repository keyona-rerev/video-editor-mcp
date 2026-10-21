FROM python:3.12-slim

# ffmpeg cuts and encodes. The fonts draw captions and title cards.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1

CMD ["sh", "-c", "python -m video_editor_mcp.server --transport streamable-http --port ${PORT:-8000}"]
