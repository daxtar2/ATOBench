# ATOBench deception-proxy sidecar for Harbor.
#
# Runs mitmdump in reverse-proxy mode in front of the juice-shop sidecar and
# applies the task's frozen RuntimeProgram on the response path. The agent
# (main container) can only reach the target through this proxy: juice-shop
# sits on an internal backend network shared with the proxy only.
#
# The vendored atobench package under ./atobench/ is produced by
# harbor/tasks/sync.sh from runtime/atobench — do not edit it in place.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# mitmproxy 10.x pulls passlib 1.7.4 which is incompatible with bcrypt>=4.0;
# pin bcrypt<4.0 so mitmdump starts cleanly (carried over from
# runtime/atobench/proxy/Dockerfile).
RUN pip install --no-cache-dir \
        "mitmproxy==10.4.2" \
        "PyYAML==6.0.3" \
        "jsonschema==4.26.0" \
        "bcrypt<4.0"

WORKDIR /opt/atobench
COPY atobench/ /opt/atobench/atobench/
COPY runtime_program.yaml /programs/runtime_program.yaml

ENV PYTHONPATH=/opt/atobench \
    ATOBENCH_RUNTIME_PROGRAM=/programs/runtime_program.yaml \
    ATOBENCH_LOG_DIR=/logs/proxy \
    ATOBENCH_LOG_BODY_LIMIT=none

EXPOSE 8080

CMD ["mitmdump", \
     "-s", "/opt/atobench/atobench/proxy/addon.py", \
     "--listen-host", "0.0.0.0", \
     "--listen-port", "8080", \
     "--mode", "reverse:http://juice-shop:3000", \
     "--set", "stream_large_body_strings=1", \
     "--set", "http2=false"]
