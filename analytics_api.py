"""Minimal read-only REST API for Channel Yield Analytics.

Prototype only. Standard library HTTP server; one configured client per instance.
Set CY_API_TOKEN, CY_CLIENT_ID, CY_DB_PATH before starting.
"""
from __future__ import annotations
import hmac, json, os, re, argparse
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from analytics_service import compute_analytics

API_VERSION = "v1"
MAX_BODY_BYTES = 16_384

class Handler(BaseHTTPRequestHandler):
    server_version = "ChannelYieldAPI/0.1"

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        expected = os.environ.get("CY_API_TOKEN", "")
        supplied = self.headers.get("Authorization", "")
        if not expected or not supplied.startswith("Bearer "):
            return False
        return hmac.compare_digest(supplied[7:].strip(), expected)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._json(200, {"status": "ok", "service": "channel-yield-analytics-api", "api_version": API_VERSION})
            return
        if path != "/v1/analytics":
            self._json(404, {"error": "not_found", "message": "Endpoint not found"})
            return
        if not self._authorized():
            self._json(401, {"error": "unauthorized", "message": "A valid bearer token is required"})
            return
        client_id = os.environ.get("CY_CLIENT_ID", "")
        db_path = os.environ.get("CY_DB_PATH", "")
        if not client_id or not db_path:
            self._json(503, {"error": "service_not_configured", "message": "CY_CLIENT_ID and CY_DB_PATH must be configured"})
            return
        q = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        allowed = {"requisition_id", "cohort_start", "cohort_end", "as_of_date", "observation_days", "min_sample_size"}
        extra = set(q) - allowed
        if extra:
            self._json(400, {"error": "unknown_parameters", "parameters": sorted(extra)})
            return
        def one(name, default=None):
            values = q.get(name)
            if values is None: return default
            if len(values) != 1: raise ValueError(f"Parameter {name} must be supplied once")
            return values[0]
        try:
            start = one("cohort_start", "2026-01-01")
            end = one("cohort_end", "2026-06-30")
            as_of = one("as_of_date", date.today().isoformat())
            # Strict ISO dates; service performs chronology checks.
            for field, value in (("cohort_start", start), ("cohort_end", end), ("as_of_date", as_of)):
                try: date.fromisoformat(value)
                except Exception: raise ValueError(f"{field} must use YYYY-MM-DD")
            observation_days = int(one("observation_days", "90"))
            min_raw = one("min_sample_size")
            min_sample_size = int(min_raw) if min_raw is not None else None
            if observation_days < 0: raise ValueError("observation_days must be >= 0")
            if min_sample_size is not None and min_sample_size < 1: raise ValueError("min_sample_size must be >= 1")
            requisition_id = one("requisition_id")
            if requisition_id is not None and not requisition_id.strip(): raise ValueError("requisition_id cannot be blank")
            result = compute_analytics(Path(db_path), client_id=client_id, cohort_start=start,
                cohort_end=end, as_of_date=as_of, observation_days=observation_days,
                requisition_id=requisition_id, min_sample_size=min_sample_size)
            if result.get("status") == "not_found":
                self._json(404, result); return
            self._json(200, result)
        except (ValueError, TypeError) as exc:
            self._json(400, {"error": "invalid_request", "message": str(exc)})
        except Exception:
            # Do not leak DB paths, SQL details, or internals to the caller.
            self._json(500, {"error": "internal_error", "message": "Analytics request failed; check server logs"})

    def do_POST(self):
        self._json(405, {"error": "method_not_allowed", "message": "This API is read-only; use GET /v1/analytics"})

    def log_message(self, fmt, *args):
        # Avoid logging request lines, which may contain requisition IDs in query strings.
        status = args[1] if len(args) > 1 else "unknown"
        print(f"client={self.client_address[0]} status={status}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("CY_API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CY_API_PORT", "8080")))
    args = parser.parse_args()
    if not os.environ.get("CY_API_TOKEN"):
        raise SystemExit("Set CY_API_TOKEN to a strong secret before starting the API")
    if not os.environ.get("CY_CLIENT_ID") or not os.environ.get("CY_DB_PATH"):
        raise SystemExit("Set CY_CLIENT_ID and CY_DB_PATH before starting the API")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Channel Yield API listening on {args.host}:{args.port}; client scope is server-configured")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == "__main__": main()
