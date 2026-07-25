# OpenPDFSpecs backend — FastAPI + MCP + in-process ingestion worker.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Tesseract with English/Hindi/Sanskrit language data for the OCR path.
# (PyMuPDF, psycopg[binary], numpy, pillow, cryptography all ship manylinux wheels,
#  so no compiler/build deps are needed.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-hin \
        tesseract-ocr-san \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ ./src/

# Render/most PaaS inject $PORT; default to 8000 locally. Bind all interfaces so the
# platform can route to the container. The ingestion worker starts via the app lifespan.
EXPOSE 8000
CMD ["sh", "-c", "uvicorn src.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
