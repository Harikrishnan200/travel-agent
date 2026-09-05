# One image runs everything: the API, the MCP Gateway and the four MCP servers.
# They are separate processes, and SPLIT_PROCESSES decides whether they talk
# over HTTP or in-process - but they are one deployable, because five images to
# demonstrate MCP would be five times the operations for none of the learning.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY gateway ./gateway
COPY mcp_servers ./mcp_servers
COPY evaluation ./evaluation
COPY scripts ./scripts

# Run as a normal user: nothing here needs root.
RUN useradd --create-home --uid 1000 traveller && chown -R traveller:traveller /app
USER traveller

EXPOSE 8000 9000 9101 9102 9103 9104

# Overridden in docker-compose for the gateway and MCP processes.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
