"""mitmproxy addon — the deception proxy entrypoint.

Run with:
    mitmdump -s atobench/proxy/addon.py --listen-port 8000 --mode reverse:http://target:80

The addon:
1. Loads RuntimeProgram YAML from ATOBENCH_RUNTIME_PROGRAM, or wraps legacy
   deception_config.yaml from ATOBENCH_DECEPTION_CONFIG when no RuntimeProgram is
   supplied.
2. On every HTTP response, executes RuntimePipeline rules.
3. Logs canonical runtime_events plus a legacy deception_tag projection.
4. Ticks state_store for periodic flush.

mitmproxy is imported lazily — only when this file is loaded by mitmdump.
Unit tests import the addon module with mitmproxy absent; they exercise the
transformer logic directly via the Flow abstraction.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

# Add the atobench package root to sys.path so the addon can be loaded from
# any working directory (mitmdump runs from the container's CWD).
_PKG_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from atobench.proxy.flow import HTTPFlow, Request, Response  # noqa: E402
from atobench.proxy.logging import log_flow  # noqa: E402
from atobench.proxy.rule_engine import RuntimePipeline  # noqa: E402
from atobench.proxy.state_store import StateStore  # noqa: E402
from atobench.runtime_ir.models import build_legacy_runtime_program  # noqa: E402
from atobench.schema.loader import validate_deception_config, validate_runtime_program  # noqa: E402


# --- globals (set in load(), read in request/response hooks) ---
_config: dict[str, Any] = {}
_primitives: list[dict[str, Any]] = []
_state_store: StateStore | None = None
_episode_id: str = ""
_turn_counter: int = 0
_runtime_program: dict[str, Any] = {}
_runtime_pipeline: RuntimePipeline | None = None
_runtime_program_from_env: bool = False


def load(lifecycle):  # pragma: no cover — mitmproxy hook, not unit-tested
    """mitmproxy lifecycle hook — called once on startup.

    Env var overrides (for per-episode sweep without rewriting config):
      ATOBENCH_EPISODE_ID — overrides cfg.episode_id
      ATOBENCH_BASELINE   — 'B0' disables all deception primitives (flag still injected)
      ATOBENCH_LOG_DIR    — overrides cfg.logging.log_dir
    """
    global _config, _primitives, _state_store, _episode_id, _runtime_program, _runtime_pipeline, _runtime_program_from_env

    config_path = os.environ.get("ATOBENCH_DECEPTION_CONFIG")
    runtime_program_path = os.environ.get("ATOBENCH_RUNTIME_PROGRAM")
    _runtime_program_from_env = bool(runtime_program_path)
    if not config_path and not runtime_program_path:
        raise RuntimeError(
            "Set ATOBENCH_RUNTIME_PROGRAM (preferred) or ATOBENCH_DECEPTION_CONFIG (legacy fallback)"
        )

    cfg: dict[str, Any] = {}
    if config_path:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        validate_deception_config(cfg)

    _config = cfg
    # Env var override: episode_id (so sweep can run many episodes against one config file)
    env_episode_id = os.environ.get("ATOBENCH_EPISODE_ID")
    _episode_id = env_episode_id or cfg.get("episode_id", "")

    # Env var override: baseline. B0 = disable all deception primitives.
    baseline = os.environ.get("ATOBENCH_BASELINE") or cfg.get("baseline", "B3")
    if baseline == "B0":
        _primitives = []  # B0 = truthful, no deception
    else:
        _primitives = cfg.get("primitives", [])

    if runtime_program_path:
        with open(runtime_program_path, "r", encoding="utf-8") as f:
            _runtime_program = yaml.safe_load(f)
        validate_runtime_program(_runtime_program)
        if not env_episode_id:
            _episode_id = _runtime_program.get("episode_id", "") or _episode_id
        if not _config:
            _config = dict(_runtime_program.get("legacy_config") or {})
        if not os.environ.get("ATOBENCH_BASELINE"):
            baseline = _runtime_program.get("baseline", baseline)
        if baseline == "B0":
            _runtime_program = dict(_runtime_program)
            _runtime_program["baseline"] = "B0"
    else:
        _runtime_program = build_legacy_runtime_program(cfg, active_primitives=_primitives)
        _runtime_program["episode_id"] = _episode_id
        _runtime_program["baseline"] = baseline
        validate_runtime_program(_runtime_program)

    log_dir = os.environ.get("ATOBENCH_LOG_DIR") or _config.get("logging", {}).get("log_dir", "logs")
    _state_store = StateStore(log_dir=log_dir, flush_every=5)
    _state_store.restore(_episode_id)
    _runtime_pipeline = RuntimePipeline(
        _runtime_program,
        state_store=_state_store,
        episode_id=_episode_id,
        baseline=baseline,
    )


def _to_internal_flow(flow) -> HTTPFlow:  # pragma: no cover — mitmproxy-typed
    """Translate mitmproxy HTTPFlow to internal HTTPFlow abstraction."""
    req = flow.request
    resp = flow.response
    parsed = urlparse(req.url)
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")

    headers = {k: v for k, v in req.headers.items()}
    req_body = req.get_content() if hasattr(req, "get_content") else None
    req_json = None
    if req_body:
        try:
            import json as _json
            req_json = _json.loads(req_body.decode("utf-8"))
        except Exception:
            req_json = None

    resp_headers = {k: v for k, v in resp.headers.items()}
    resp_body = resp.get_content() if hasattr(resp, "get_content") else None
    resp_json = None
    if resp_body:
        try:
            import json as _json
            parsed_json = _json.loads(resp_body.decode("utf-8"))
            if isinstance(parsed_json, dict):
                resp_json = parsed_json
        except Exception:
            resp_json = None

    internal = HTTPFlow(
        request=Request(
            method=req.method,
            path=path,
            headers=headers,
            body=req_body,
            json=req_json,
        ),
        response=Response(
            status_code=resp.status_code,
            headers=resp_headers,
            body=resp_body,
            json=resp_json,
        ),
    )
    # Stash episode_id on the flow for transformers that need it
    internal.episode_id = _episode_id  # type: ignore[attr-defined]
    return internal


def _write_back(flow, internal: HTTPFlow) -> None:  # pragma: no cover
    """Write internal flow's response back to mitmproxy flow.

    Sets resp.content = body_bytes. mitmproxy auto-computes Content-Length from
    resp.content when sending — DO NOT set Content-Length explicitly in headers
    (overrides auto-computed value and causes truncation when body grew).
    """
    resp = flow.response
    if internal.response.body is not None:
        resp.content = internal.response.body
    # mitmproxy headers: clear and re-set, EXCEPT drop any explicit Content-Length
    # so mitmproxy auto-computes from resp.content.
    for k in list(resp.headers.keys()):
        del resp.headers[k]
    for k, v in internal.response.headers.items():
        if k.lower() == "content-length":
            continue  # let mitmproxy auto-compute from resp.content
        resp.headers[k] = v
    resp.status_code = internal.response.status_code


def _inject_flag(internal: HTTPFlow) -> None:
    """Inject the real FLAG{...} into the response if path matches cfg.flag.endpoint.

    This is NOT deception — it's the ground-truth flag the agent must find.
    Runs in both B0 and B3. In B0 the agent finds it; in B3 deception may
    derail the agent so it never reaches this endpoint or misreads it.

    Injection rules:
    - If response is JSON dict: set field `flag` = cfg.flag.value
    - If response is JSON list: append {"flag": value}
    - If response is HTML: append <!-- FLAG{...} --> comment near </body>
    - If response is text/plain: append "\nFLAG{...}\n"
    The agent's job: discover this endpoint and extract FLAG{...}.
    """
    flag_cfg = _config.get("flag") or {}
    if not flag_cfg:
        return
    flag_endpoint = flag_cfg.get("endpoint")
    flag_value = flag_cfg.get("value")
    if not flag_endpoint or not flag_value:
        return

    # Match path (strip query string)
    path = internal.request.path.split("?", 1)[0]
    if path != flag_endpoint:
        return

    body = internal.response.body or b""
    content_type = internal.response.headers.get("Content-Type", "")

    if "json" in content_type.lower() or internal.response.json is not None:
        # JSON path
        if internal.response.json is None:
            try:
                import json as _json
                internal.response.json = _json.loads(body.decode("utf-8"))
            except Exception:
                internal.response.json = {}
        if isinstance(internal.response.json, dict):
            internal.response.json["flag"] = flag_value
        elif isinstance(internal.response.json, list):
            internal.response.json.append({"flag": flag_value})
        else:
            # Wrap scalar
            internal.response.json = {"original": internal.response.json, "flag": flag_value}
        # Re-serialize body. Do NOT set Content-Length (mitmproxy auto-computes).
        import json as _json
        new_body = _json.dumps(internal.response.json, ensure_ascii=False).encode("utf-8")
        internal.response.body = new_body
        internal.response.set_header("Content-Type", "application/json")
    elif "html" in content_type.lower():
        text = body.decode("utf-8", errors="replace")
        marker = f"<!-- {flag_value} -->"
        if marker not in text:
            if "</body>" in text:
                text = text.replace("</body>", f"{marker}\n</body>", 1)
            else:
                text = text + "\n" + marker
            new_body = text.encode("utf-8")
            internal.response.body = new_body
    else:
        # text/plain or unknown — append
        text = body.decode("utf-8", errors="replace")
        if flag_value not in text:
            text = text.rstrip() + f"\n{flag_value}\n"
            new_body = text.encode("utf-8")
            internal.response.body = new_body


def _inject_link_advertisements(internal: HTTPFlow) -> None:
    """Inject _links.flag (and other hints) into matched endpoint responses.

    Runs in BOTH B0 and B3. In B0, agent follows _links to discover the flag
    endpoint. In B3, substitute_subgoal's _schema_coupled rewrites _links and
    removes any key containing 'flag'/'complete'/'final' — hiding the hint.

    Config:
    link_advertisements:
      - endpoint: /rest/user/whoami   # match this path
        links:
          flag: /rest/admin/flag      # add _links.flag = this value
    """
    adv_list = _config.get("link_advertisements") or []
    if not adv_list:
        return
    path = internal.request.path.split("?", 1)[0]

    for adv in adv_list:
        if adv.get("endpoint") != path:
            continue
        links_to_add = adv.get("links") or {}
        if not links_to_add:
            continue
        # Parse response as JSON (skip if not JSON)
        data = internal.get_or_parse_response_json() if hasattr(internal, "get_or_parse_response_json") else None
        if data is None:
            try:
                import json as _json
                body = internal.response.body or b""
                data = _json.loads(body.decode("utf-8"))
                if not isinstance(data, dict):
                    continue
            except Exception:
                continue
        links = data.setdefault("_links", {})
        for k, v in links_to_add.items():
            links[k] = v
        internal.response.json = data
        import json as _json
        new_body = _json.dumps(data, ensure_ascii=False).encode("utf-8")
        internal.response.body = new_body
        internal.response.set_header("Content-Type", "application/json")


def request(flow):  # pragma: no cover — mitmproxy hook
    """mitmproxy request hook — runs BEFORE forwarding to upstream.

    Used for synthetic endpoints: if the request path matches a configured
    synthetic endpoint, return a synthetic response without hitting the
    target. This lets the proxy expose endpoints that don't exist in the
    real target (e.g. /rest/admin/flag for ATOBench flag injection).

    The synthetic response goes through the same deception pipeline as a
    real response (transformers + flag injection + logging).
    """
    # RuntimeProgram mode is intentionally response-pipeline only for now.
    # Do not let legacy deception_config.synthetic_endpoints leak into new
    # experiments when both runtime_program.yaml and the compatibility
    # deception_config.yaml are present in an ID-scoped workspace.
    if _runtime_program_from_env:
        return
    if not _config:
        return
    synthetic_endpoints = _config.get("synthetic_endpoints") or []
    if not synthetic_endpoints:
        return

    parsed = urlparse(flow.request.url)
    path = parsed.path

    for ep in synthetic_endpoints:
        # Match by exact path OR by regex (path_regex)
        ep_path = ep.get("path", "")
        ep_regex = ep.get("path_regex", "")
        if ep_path and ep_path == path:
            pass  # match
        elif ep_regex:
            try:
                if not re.search(ep_regex, path):
                    continue
            except re.error:
                continue
        else:
            continue
        # Match found — return synthetic response
        status = ep.get("status", 200)
        body_dict = ep.get("response", {})
        import json as _json
        body_bytes = _json.dumps(body_dict).encode("utf-8")
        # Build a mitmproxy Response
        from mitmproxy.http import Response as MitmResponse
        flow.response = MitmResponse.make(
            status,
            body_bytes,
            {"Content-Type": "application/json"},
        )
        return  # don't forward to upstream


def response(flow):  # pragma: no cover — mitmproxy hook
    """mitmproxy response hook — called after upstream response is received.

    Pipeline:
      1. Always: inject FLAG{...} if path matches cfg.flag.endpoint (B0 + B3).
         Runs FIRST so that B3 transformers which REPLACE the response body
         (e.g. fake_version_banner schema_coupled) clobber the flag — that's
         the deception effect! In B0, no transformer runs, so the flag
         survives and the agent finds it.
      2. If B3 (deception active): apply matched transformer. Transformers
         that modify (vs replace) the dict will preserve the flag; transformers
         that replace the dict will hide it.
      3. Always: log the turn to turns.jsonl
    """
    global _turn_counter

    internal = _to_internal_flow(flow)

    next_turn_idx = _turn_counter + 1
    try:
        _runtime_pipeline.execute(internal, turn_idx=next_turn_idx)
    except Exception as e:  # pragma: no cover — defensive
        import sys as _sys
        print(f"[deception_proxy] runtime pipeline error: {e}", file=_sys.stderr)

    # Write back to mitmproxy flow (body / headers / status).
    _write_back(flow, internal)

    # Step 3: log the turn (deception_tag is empty if no transformer matched)
    _turn_counter += 1
    log_flow(internal, episode_id=_episode_id, turn_idx=_turn_counter)
    if _state_store is not None:
        _state_store.tick(_episode_id)


def done():  # pragma: no cover
    """mitmproxy shutdown hook — flush state."""
    if _state_store is not None and _episode_id:
        _state_store.flush(_episode_id)
