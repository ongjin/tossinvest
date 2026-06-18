# deploy/Dockerfile — build context is the repo root (uv workspace monorepo)
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir uv

# copy the whole workspace (root pyproject + both member packages)
COPY . .
RUN uv sync --package pytossinvest-mcp --extra redis --extra http --frozen

# http mode, bind all interfaces inside the container, redis state backend
ENV TOSSINVEST_TRANSPORT=http \
    TOSSINVEST_HTTP_HOST=0.0.0.0 \
    TOSSINVEST_HTTP_PORT=8000 \
    TOSSINVEST_STATE_BACKEND=redis

EXPOSE 8000
CMD ["uv", "run", "--package", "pytossinvest-mcp", "pytossinvest-mcp"]
