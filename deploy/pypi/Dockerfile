# deploy/pypi/Dockerfile — pytossinvest-mcp installed from PyPI, every dependency pinned by hash.
# The build context is this directory: nothing from the source tree goes into the image.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --require-hashes -r requirements.txt && pip check

ENV TOSSINVEST_TRANSPORT=http \
    TOSSINVEST_HTTP_HOST=0.0.0.0 \
    TOSSINVEST_HTTP_PORT=8000 \
    TOSSINVEST_STATE_BACKEND=memory

EXPOSE 8000
CMD ["pytossinvest-mcp"]
