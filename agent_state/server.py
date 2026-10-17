"""Long-running JSON API with explicitly selected cloud object storage."""
import argparse
import base64
import binascii
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit
from .engine import Engine
from .objects import encode
from .runtime import Runtime
from .storage import StateError, Missing, Conflict, Invalid, TooLarge
from .config import DEFAULTS, load_settings, create_store

LOG = logging.getLogger("agent_state")


def make_server(runtime, host="127.0.0.1", port=8844, token=None, bucket=None):
    if host not in {"127.0.0.1", "localhost"} and not token:
        raise Invalid("A non-loopback listener requires AGENTDB_STATE_TOKEN and a TLS-protected deployment ingress")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Avoid logging request bodies, credentials, and user-provided query text.
            LOG.debug("HTTP request completed")

        def respond(self, status, body):
            raw = encode(body)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)

        def body(self):
            if self.headers.get("Transfer-Encoding"):
                raise Invalid("Use Content-Length, not chunked request bodies")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise Invalid("Invalid Content-Length") from error
            if not 0 < length <= 1500000:
                raise TooLarge("Request body must be 1..1500000 bytes")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise Invalid("Incomplete request body")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise Invalid("JSON body must be an object")
            return value

        def dispatch(self):
            self.connection.settimeout(30)
            if token:
                expected = "Bearer " + token
                if not hmac.compare_digest(self.headers.get("Authorization", "").encode("utf-8"), expected.encode("utf-8")):
                    self.respond(401, {"error": "unauthorized"})
                    return
            try:
                url = urlsplit(self.path)
                parts = url.path.strip("/").split("/")
                query = parse_qs(url.query)
                method = self.command
                if parts == ["health"] and method == "GET":
                    value = dict(status="running", backend=type(runtime.engine.store).__name__)
                elif parts == ["metrics"] and method == "GET":
                    value = runtime.metrics()
                elif parts[:2] == ["v1", "tasks"] and len(parts) >= 3:
                    task_id = parts[2]
                    if len(parts) == 3 and method == "GET":
                        value = runtime.read(task_id, fresh=query.get("fresh") == ["true"])
                    elif len(parts) == 3 and method in {"POST", "PUT"}:
                        body = self.body()
                        if method == "POST" and body.get("expected_version") is not None:
                            raise Invalid("POST creates tasks; use PUT with expected_version to update")
                        if method == "PUT" and body.get("expected_version") is None:
                            raise Invalid("PUT requires expected_version")
                        value = runtime.publish(task_id, **body)
                    elif parts[3:] == ["context"] and method == "POST":
                        value = runtime.context(task_id, **self.body())
                    elif parts[3:] == ["history"] and method == "GET":
                        value = runtime.engine.history(task_id, limit=int(query.get("limit", ["20"])[0]),
                                                       cursor=query.get("cursor", [None])[0])
                    else:
                        raise Missing("route")
                elif parts == ["v1", "artifacts"] and method == "POST":
                    data = base64.b64decode(self.body()["base64"], validate=True)
                    value = dict(ref=runtime.engine.objects.put_bytes(data), bytes=len(data))
                elif parts[:2] == ["v1", "artifacts"] and len(parts) == 3 and method == "GET":
                    data = runtime.engine.objects.get_bytes(parts[2])
                    value = dict(ref=parts[2], base64=base64.b64encode(data).decode("ascii"), bytes=len(data))
                elif parts[:2] == ["v1", "objects"] and len(parts) == 3 and method == "GET":
                    value = runtime.engine.objects.get(parts[2])
                elif parts == ["v1", "notifications"] and method == "POST":
                    payload = self.body()
                    if "event" in payload:
                        invalidated = runtime.notify_event(payload["event"], bucket=bucket)
                    elif getattr(runtime.engine.store, "provider", "gcs") == "gcs":
                        invalidated = runtime.notify(payload.get("attributes"), bucket=bucket)
                    else:
                        raise Invalid("This backend requires a normalized notification event")
                    value = dict(invalidated=invalidated)
                else:
                    raise Missing("route")
                self.respond(200, value)
            except StateError as error:
                status = 404 if isinstance(error, Missing) else 409 if isinstance(error, Conflict) else 422 if isinstance(error, Invalid) else 500
                self.respond(status, dict(error=error.code, message=str(error)))
            except (ValueError, TypeError, KeyError, binascii.Error, RecursionError) as error:
                self.respond(400, dict(error="invalid_request", message=str(error)))
            except Exception:
                LOG.exception("State service request failed")
                self.respond(503, dict(error="backend_unavailable", message="Operation outcome may be unknown; retry the same command_id after checking connectivity."))

        do_GET = dispatch
        do_POST = dispatch
        do_PUT = dispatch

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--bucket")
    parser.add_argument("--provider", choices=["gcs", "oss", "s3"])
    parser.add_argument("--project")
    parser.add_argument("--region")
    parser.add_argument("--endpoint")
    parser.add_argument("--prefix")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--subscription")
    parser.add_argument("--tokenizer")
    parser.add_argument("--check", action="store_true", help="Check bucket access/configuration; do not start a server")
    args = parser.parse_args()
    try:
        settings = load_settings(args.config, **{key: getattr(args, key) for key in DEFAULTS})
    except (Invalid, ValueError) as error:
        parser.error(str(error))
    logging.basicConfig(level=logging.INFO)
    stop = threading.Event()
    subscriber = future = server = worker = None
    try:
        store = create_store(settings)
        checked = store.check()
        if args.check:
            print(json.dumps(checked))
            return
        counter = None
        if settings["tokenizer"]:
            import tiktoken
            encoding = tiktoken.get_encoding(settings["tokenizer"])
            counter = lambda text: len(encoding.encode(text, disallowed_special=()))
        runtime = Runtime(Engine(store, settings["prefix"]), token_counter=counter,
                          tokenizer_name=settings["tokenizer"])
        server = make_server(runtime, settings["host"], settings["port"],
                             os.environ.get("AGENTDB_STATE_TOKEN"), settings["bucket"])

        def refresh():
            while not stop.wait(30):
                result = runtime.refresh_active()
                if result["errors"]:
                    LOG.warning("Some active task refreshes failed: %s", result)
        worker = threading.Thread(target=refresh, daemon=True)
        worker.start()
        if settings["subscription"]:
            from google.cloud import pubsub_v1
            subscriber = pubsub_v1.SubscriberClient()

            def receive(message):
                try:
                    runtime.notify(dict(message.attributes), bucket=settings["bucket"])
                    message.ack()
                except Invalid:
                    LOG.warning("Discarding malformed notification")
                    message.ack()
                except Exception:
                    message.nack()
            future = subscriber.subscribe(settings["subscription"], callback=receive)
        LOG.info("Resident state service listening on %s:%s using %s", settings["host"], settings["port"], settings["provider"])
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    except Exception as error:
        parser.exit(1, f"Unable to start {settings['provider']} state service: {error}\n")
    finally:
        stop.set()
        if future:
            future.cancel()
        if subscriber:
            subscriber.close()
        if server:
            server.server_close()
        if worker:
            worker.join(timeout=1)


if __name__ == "__main__":
    main()
