"""Loopback-only resident gateway: agent_lab.py worker actions on reused db.Session processes.

Opt-in. agent_lab.py consults it only when AGENTDB_LAB_GATEWAY names the
rendezvous file this process writes; with no file (gateway not running) or a
refused connection, agent_lab.py runs exactly as before. A request is the
agent_lab argv itself: the gateway parses and validates it with agent_lab's own
parser and checks, then runs agent_lab.perform on a pooled Session for the same
connection settings, agentdb_client role and database. It never retries; an
interrupted request is reported as having an unknown outcome.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

# The client runs once per tool call, so it imports only what it needs: on the
# development laptop http.client and the server modules cost 150-250 ms each start.

HOST = "127.0.0.1"
REQUEST_LIMIT = 65536
RESPONSE_LIMIT = 4 * 1024 * 1024
# Longer than one 120 s statement timeout; the harness bounds the whole call anyway.
CLIENT_TIMEOUT = 130


class Rejected(Exception):
    """An argument error, carrying the text argparse would print."""


class Busy(Exception):
    pass


class RequestParser(argparse.ArgumentParser):
    def error(self, message):
        raise Rejected(self.format_usage() + f"{self.prog}: error: {message}\n")

    def exit(self, status=0, message=None):
        raise Rejected(message or "")


def fingerprint(connection):
    """What decides where and how psql connects; never a password value."""
    env = {key: ("<set>" if key == "PGPASSWORD" else value) for key, value in connection.env.items()
           if key.startswith("PG") or key == "WSLENV"}
    return json.loads(json.dumps(dict(settings=connection.settings, role=connection.role,
                                      prefix=[str(part) for part in connection.prefix], env=env)))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def run(rendezvous, argv, connection):
    """Client side: (stdout, stderr, exit_code), or None when no gateway is running."""
    try:
        raw = rendezvous.read_bytes()
    except FileNotFoundError:
        return None
    unavailable = "gateway_unavailable: {}; nothing was executed\n"
    try:
        info = json.loads(raw)
        port, token = int(info["port"]), str(info["token"])
        if info["host"] != HOST or not 0 < port < 65536:
            raise ValueError("not a loopback gateway")
    except (OSError, ValueError, KeyError, TypeError) as error:
        return "", unavailable.format(f"unreadable rendezvous file {rendezvous} ({error})"), 1
    body = json.dumps(dict(argv=list(argv), cwd=os.getcwd(), fingerprint=fingerprint(connection))).encode("utf-8")
    request = (f"POST /v1/run HTTP/1.1\r\nHost: {HOST}:{port}\r\nAuthorization: Bearer {token}\r\n"
               f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n")
    unknown = "gateway_outcome_unknown: {}; the action may have run. Not retried: read current state before repeating it.\n"
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(CLIENT_TIMEOUT)
    try:
        try:
            client.connect((HOST, port))
        except ConnectionRefusedError:
            return None  # Nothing was sent: a stale file left by a gateway that is gone.
        except OSError as error:
            return "", unavailable.format(error), 1
        received, size = [], 0
        try:
            client.sendall(request.encode("ascii") + body)
            while size <= RESPONSE_LIMIT:
                chunk = client.recv(65536)
                if not chunk:
                    break
                received.append(chunk)
                size += len(chunk)
        except OSError as error:
            return "", unknown.format(repr(error)), 1
    finally:
        client.close()
    try:
        # The server answers HTTP/1.0 with Content-Length and closes the connection.
        head, separator, data = b"".join(received).partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split()[1]) if separator else None
        headers = {name.strip().lower(): value.strip() for name, _, value in (line.partition(":") for line in lines[1:])}
        if status is None or len(data) > RESPONSE_LIMIT or int(headers.get("content-length", -1)) != len(data):
            raise ValueError("no complete response")
        value = json.loads(data)
        if status == 200:
            stdout, stderr, code = value["stdout"], value["stderr"], value["exit_code"]
            if isinstance(stdout, str) and isinstance(stderr, str) and code in (0, 1, 2):
                return stdout, stderr, code
            raise ValueError("invalid result")
        error, message = str(value["error"]), str(value["message"])
    except (ValueError, KeyError, TypeError, IndexError) as problem:
        return "", unknown.format(problem), 1
    if status in (400, 401, 409, 503):
        return "", f"{error}: {message}; nothing was executed\n", 1
    return "", unknown.format(f"{error}: {message}"), 1


class Pool:
    """Idle Sessions per (connection fingerprint, database), at most max_sessions open.

    A Session serves one request at a time and resets all session state after
    every call, so which idle Session a request gets does not change its result.
    """

    def __init__(self, factory, max_sessions, clock):
        self.factory, self.max_sessions, self.clock = factory, max_sessions, clock
        self.idle, self.open, self.created = {}, 0, 0
        self.lock = threading.Lock()

    def acquire(self, key, connection, database):
        victim = None
        with self.lock:
            if self.idle.get(key):
                return self.idle[key].pop()[0]
            if self.open >= self.max_sessions:
                oldest = min(((entries[0][1], name) for name, entries in self.idle.items() if entries), default=None)
                if oldest is None:
                    raise Busy(f"all {self.max_sessions} sessions are in use")
                victim = self.idle[oldest[1]].pop(0)[0]
            else:
                self.open += 1
        if victim is not None:
            victim.close()
        try:
            session = self.factory(connection, database)
        except BaseException:
            with self.lock:
                self.open -= 1
            raise
        with self.lock:
            self.created += 1
        return session

    def release(self, key, session):
        with self.lock:
            self.idle.setdefault(key, []).append((session, self.clock()))

    def reap(self, idle_timeout):
        now, expired = self.clock(), []
        with self.lock:
            for key, entries in self.idle.items():
                expired += [session for session, used in entries if now - used >= idle_timeout]
                entries[:] = [(session, used) for session, used in entries if now - used < idle_timeout]
            self.open -= len(expired)
        for session in expired:
            session.close()
        return len(expired)

    def close_all(self):
        with self.lock:
            sessions = [session for entries in self.idle.values() for session, _ in entries]
            self.idle.clear()
            self.open -= len(sessions)
        for session in sessions:
            session.close()

    def counts(self):
        with self.lock:
            return dict(open=self.open, idle=sum(map(len, self.idle.values())), created=self.created)


class Gateway:
    def __init__(self, rendezvous, port=0, idle_timeout=600, max_sessions=8, session_factory=None,
                 clock=time.monotonic, log=None):
        if not 1 <= idle_timeout <= 3600:
            raise ValueError("idle timeout must be 1..3600 seconds")
        if not 1 <= max_sessions <= 32:
            raise ValueError("max sessions must be 1..32")
        from http.server import ThreadingHTTPServer
        import secrets
        import agent_lab  # The serving side only; the client runs inside agent_lab.
        self.agent_lab, self.rendezvous = agent_lab, Path(rendezvous)
        self.idle_timeout, self.clock, self.log = idle_timeout, clock, log
        self.pool = Pool(session_factory or (lambda connection, database: connection.session(database)),
                         max_sessions, clock)
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.inflight, self.requests, self.last_activity = 0, 0, clock()
        self.stopped = threading.Event()
        self.server = ThreadingHTTPServer((HOST, port), self._handler())
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.published = False

    def publish(self):
        # O_EXCL: never overwrite another gateway's file, live or stale.
        descriptor = os.open(self.rendezvous, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(host=HOST, port=self.port, token=self.token, pid=os.getpid()), handle)
        self.published = True

    def serve_forever(self):
        reaper = threading.Thread(target=self._reap, daemon=True)
        reaper.start()
        try:
            self.server.serve_forever(poll_interval=0.2)
        finally:
            self.close()

    def _reap(self):
        interval = min(max(self.idle_timeout / 4, 0.05), 30)
        while not self.stopped.wait(interval):
            self.pool.reap(self.idle_timeout)
            with self.lock:
                idle = self.inflight == 0 and self.clock() - self.last_activity >= self.idle_timeout
            if idle:
                self.server.shutdown()
                return

    def close(self):
        self.stopped.set()
        if self.published:
            # Remove the file first so new calls go back to agent_lab's own path.
            try:
                if json.loads(self.rendezvous.read_bytes()).get("token") == self.token:
                    self.rendezvous.unlink()
            except (OSError, ValueError, AttributeError):
                pass
            self.published = False
        self.server.server_close()
        self.pool.close_all()

    def health(self):
        with self.lock:
            value = dict(status="running", requests=self.requests, inflight=self.inflight)
        return dict(value, sessions=self.pool.counts(), idle_timeout_seconds=self.idle_timeout)

    def execute(self, body):
        import traceback
        argv, cwd, theirs = body["argv"], body["cwd"], body["fingerprint"]
        if (not isinstance(argv, list) or not all(isinstance(item, str) for item in argv) or len(argv) > 64
                or not isinstance(cwd, str) or not Path(cwd).is_absolute() or not isinstance(theirs, dict)):
            raise ValueError("argv must be a list of strings, cwd an absolute path, fingerprint an object")
        lab = self.agent_lab
        parser = lab.build_parser(RequestParser, prog="agent_lab.py", add_help=False)
        try:
            args = parser.parse_args(argv)
            if args.action == "init" or args.password:
                raise ValueError("init and --password run without the gateway")
            if args.config is not None and not args.config.is_absolute():
                args.config = Path(cwd) / args.config
            lab.check_database(parser, args)
            connection = lab.connect(args, args.database, "agentdb_client")
            mine = fingerprint(connection)
            if canonical(mine) != canonical(theirs):
                raise Mismatch("the gateway would connect with different settings or environment than this client")
            lab.validate(parser, args)
        except Rejected as error:
            return dict(stdout="", stderr=str(error), exit_code=2), args_action(argv)
        except (ValueError, Mismatch):
            raise
        except Exception:
            return dict(stdout="", stderr=traceback.format_exc(), exit_code=1), args_action(argv)
        key = canonical(mine) + "\0" + args.database
        session = self.pool.acquire(key, connection, args.database)
        try:
            stdout, code = lab.perform(args, session)
            result = dict(stdout=stdout, stderr="", exit_code=code)
        except Exception:
            result = dict(stdout="", stderr=traceback.format_exc(), exit_code=1)
        finally:
            self.pool.release(key, session)
        return result, args.action

    def _handler(self):
        from http.server import BaseHTTPRequestHandler
        import hmac
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass  # Request bodies hold policy code; log only the summary below.

            def respond(self, status, value):
                raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(raw)

            def authorized(self):
                expected = ("Bearer " + gateway.token).encode("utf-8")
                return hmac.compare_digest(self.headers.get("Authorization", "").encode("utf-8"), expected)

            def do_GET(self):
                self.connection.settimeout(30)
                if not self.authorized():
                    self.respond(401, dict(error="unauthorized", message="missing or wrong gateway token"))
                elif self.path == "/health":
                    self.respond(200, gateway.health())
                else:
                    self.respond(404, dict(error="not_found", message="route"))

            def do_POST(self):
                self.connection.settimeout(30)
                # Read the bounded body before any answer: closing a socket with
                # unread data sends RST on Windows, and the client loses the reply.
                try:
                    if self.headers.get("Transfer-Encoding"):
                        raise ValueError("Use Content-Length, not chunked request bodies")
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= REQUEST_LIMIT:
                        raise ValueError(f"Request body must be 1..{REQUEST_LIMIT} bytes")
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError("Incomplete request body")
                except (ValueError, OSError) as error:
                    self.respond(400, dict(error="invalid_request", message=str(error)))
                    return
                if not self.authorized():
                    self.respond(401, dict(error="unauthorized", message="missing or wrong gateway token"))
                    return
                if self.path != "/v1/run":
                    self.respond(404, dict(error="not_found", message="route"))
                    return
                try:
                    body = json.loads(raw)
                    if not isinstance(body, dict):
                        raise ValueError("JSON body must be an object")
                except ValueError as error:
                    self.respond(400, dict(error="invalid_request", message=str(error)))
                    return
                started = time.perf_counter()
                with gateway.lock:
                    gateway.inflight += 1
                    gateway.requests += 1
                try:
                    result, action = gateway.execute(body)
                    status = 200
                except Busy as error:
                    status, result, action = 503, dict(error="gateway_busy", message=str(error)), "?"
                except Mismatch as error:
                    status, result, action = 409, dict(error="gateway_mismatch", message=str(error)), "?"
                except (ValueError, KeyError, TypeError) as error:
                    status, result, action = 400, dict(error="invalid_request", message=str(error)), "?"
                finally:
                    with gateway.lock:
                        gateway.inflight -= 1
                        gateway.last_activity = gateway.clock()
                if gateway.log:
                    gateway.log(f"action={action} status={status} exit={result.get('exit_code')} "
                                f"ms={(time.perf_counter() - started) * 1000:.1f}")
                self.respond(status, result)

        return Handler


class Mismatch(Exception):
    pass


def args_action(argv):
    return next((item for item in argv if not item.startswith("-")), "?")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rendezvous", type=Path, default=os.environ.get("AGENTDB_LAB_GATEWAY"),
                        help="File to write port and token to; default AGENTDB_LAB_GATEWAY")
    parser.add_argument("--port", type=int, default=0, help="Loopback port; default an ephemeral one")
    parser.add_argument("--idle-timeout", type=float, default=600,
                        help="Close a Session idle this long, and exit when no request arrived for as long (1..3600 s)")
    parser.add_argument("--max-sessions", type=int, default=8, help="Open Sessions across all databases (1..32)")
    args = parser.parse_args()
    if not args.rendezvous:
        parser.error("--rendezvous or AGENTDB_LAB_GATEWAY is required")
    try:
        gateway = Gateway(args.rendezvous, args.port, args.idle_timeout, args.max_sessions,
                          log=lambda line: print(line, file=sys.stderr, flush=True))
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")
    try:
        gateway.publish()
    except FileExistsError:
        gateway.close()
        parser.exit(1, f"{args.rendezvous} exists: another gateway is running, or remove the stale file\n")
    print(json.dumps(dict(listening=f"{HOST}:{gateway.port}", rendezvous=str(args.rendezvous),
                          pid=os.getpid(), idle_timeout_seconds=args.idle_timeout,
                          max_sessions=args.max_sessions)), flush=True)
    try:
        gateway.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
