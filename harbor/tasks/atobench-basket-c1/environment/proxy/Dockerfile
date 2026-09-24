# ATOBench deception-proxy sidecar for the basket AOU (Harbor).
#
# Same proxy runtime as the SQLi tasks, plus the seed assets used by the
# short-lived `seed` service: the target-state contract, the frozen program
# template, and seed.py, which reseeds the fresh per-trial target and
# materializes the RuntimeProgram into the shared /program volume before
# mitmdump starts.
#
# The vendored atobench package under ./atobench/ is produced by
# harbor/tasks/sync.sh from runtime/atobench — do not edit it in place.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# mitmproxy 10.x pulls passlib 1.7.4 which is incompatible with bcrypt>=4.0;
# pin bcrypt<4.0 so mitmdump starts cleanly.
RUN pip install --no-cache-dir \
        "mitmproxy==10.4.2" \
        "PyYAML==6.0.3" \
        "jsonschema==4.26.0" \
        "bcrypt<4.0"

WORKDIR /opt/atobench
COPY atobench/ /opt/atobench/atobench/
COPY seed.py target_state_contract.yaml /seed/
COPY c0_shadow_template.yaml c1_treatment_template.yaml /seed/

ENV PYTHONPATH=/opt/atobench \
    ATOBENCH_RUNTIME_PROGRAM=/program/runtime_program.yaml \
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
