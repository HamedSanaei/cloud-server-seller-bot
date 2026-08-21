FROM python:3.13-slim
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv sync --no-dev
COPY . .
CMD ["uv", "run", "uvicorn", "cloud_platform.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
