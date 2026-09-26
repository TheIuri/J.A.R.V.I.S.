FROM python:3.11-slim

# WITH_CUDA=true incluye las librerias para usar la GPU NVIDIA con Whisper.
# Si no hay GPU, la imagen sigue funcionando en CPU (WHISPER_DEVICE=auto).
ARG WITH_CUDA=true

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    HF_HOME=/data/hf \
    XDG_CACHE_HOME=/data/cache \
    LD_LIBRARY_PATH=/usr/local/lib/python3.11/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib

WORKDIR /app
COPY requirements.txt requirements-cuda.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$WITH_CUDA" = "true" ]; then pip install --no-cache-dir -r requirements-cuda.txt; fi

COPY jarvis ./jarvis

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health')"
CMD ["uvicorn", "jarvis.main:app", "--host", "0.0.0.0", "--port", "8765"]
