FROM python:3.12-slim

ARG RMAPI_VERSION=v0.0.34

RUN apt-get update && apt-get install -y --no-install-recommends \
      bash curl ca-certificates unzip jq poppler-utils libcairo2 tzdata \
    && rm -rf /var/lib/apt/lists/*

# rmapi (ddvk fork) — reMarkable cloud client
RUN curl -fsSL "https://github.com/ddvk/rmapi/releases/download/${RMAPI_VERSION}/rmapi-linux-amd64.tar.gz" \
      | tar -xz -C /usr/local/bin rmapi \
    && chmod +x /usr/local/bin/rmapi

# claude CLI (native installer; auth via CLAUDE_CODE_OAUTH_TOKEN env at runtime)
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/root/.local/bin:${PATH}"

RUN pip install --no-cache-dir rmc rmscene cairosvg httpx pyyaml

WORKDIR /app
COPY rm2md.sh /app/rm2md.sh
COPY bot/rmbot.py /app/rmbot.py

ENV RM_FOLDER=Prep
CMD ["python", "-u", "/app/rmbot.py"]
