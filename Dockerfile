FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY semantic_mcp ./semantic_mcp
COPY examples ./examples
COPY scripts ./scripts
RUN pip install --no-cache-dir . && python scripts/prepare_tokenizer.py && \
    useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
ENV BIND_HOST=0.0.0.0
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
CMD ["python", "scripts/start.py"]
