# Image for the "demo" service in compose.yaml: the PostgreSQL 16 client tools plus Python,
# so the demo installs AgentDB with db.py, the same way as a local install.
FROM postgres:16
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 \
 && rm -rf /var/lib/apt/lists/*
