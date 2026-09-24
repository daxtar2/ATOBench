# Agent (main) container for the ATOBench SQLi spike task.
# curl/jq for HTTP work; python3 for payload construction and the verifier.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        jq \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*
