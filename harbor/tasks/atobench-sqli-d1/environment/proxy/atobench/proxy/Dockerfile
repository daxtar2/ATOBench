# ATOBench deception proxy container.
#
# Image: atobench-deception-proxy:latest
# Entrypoint: mitmdump -s /opt/atobench/atobench/proxy/addon.py --listen-port 8000 --mode reverse:$ATOBENCH_TARGET_BASE_URL
#
# Reads deception_config.yaml from $ATOBENCH_DECEPTION_CONFIG (mounted at runtime).
# Writes turns.jsonl + state_<ep>.json to /logs (mounted at runtime).
# Strips X-ATOBench-Tag headers before forwarding to the agent (black-box boundary).

FROM python:3.11-slim

# mitmproxy needs a recent SSL stack; the slim image has it but install ca-certificates.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install mitmproxy (stable pin) + PyYAML + jsonschema (for addon's schema validation)
# NOTE: mitmproxy 10.x pulls passlib 1.7.4 which is incompatible with bcrypt>=4.0
# (passlib looks for _bcrypt.__about__.__version__, removed in bcrypt 4+). Force
# bcrypt<4.0 so passlib's bcrypt backend initializes cleanly. Without this,
# mitmdump crashes on startup with "module 'bcrypt' has no attribute '__about__'".
RUN pip install --no-cache-dir \
        "mitmproxy==10.4.2" \
        "PyYAML==6.0.3" \
        "jsonschema==4.26.0" \
        "bcrypt<4.0"

# Copy only the atobench package (build context = atobench/ dir, set by docker-compose)
WORKDIR /opt/atobench
COPY . /opt/atobench/

# Make the atobench package importable
ENV PYTHONPATH=/opt/atobench
ENV ATOBENCH_LOG_DIR=/logs

# Default port; overridden at runtime if needed
EXPOSE 8000

# Entrypoint: caller provides ATOBENCH_TARGET_BASE_URL + ATOBENCH_DECEPTION_CONFIG
# Example docker run:
#   docker run -p 8000:8000 \
#     -e ATOBENCH_DECEPTION_CONFIG=/etc/atobench/deception_config.yaml \
#     -e ATOBENCH_TARGET_BASE_URL=http://target:3000 \
#     -v $(pwd)/deception_config.yaml:/etc/atobench/deception_config.yaml:ro \
#     -v logs:/logs \
#     atobench-deception-proxy:latest
CMD ["mitmdump", \
     "-s", "/opt/atobench/atobench/proxy/addon.py", \
     "--listen-port", "8000", \
     "--mode", "reverse:http://target:80", \
     "--set", "stream_large_body_strings=1", \
     "--set", "http2=false"]
