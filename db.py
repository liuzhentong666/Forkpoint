"""Connect to an existing local PostgreSQL server; never manage its lifecycle."""
import argparse
import getpass
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "connection.local.json"
FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
DEFAULTS = {"host": "127.0.0.1", "port": 5432, "user": "postgres",
            "database": "agentdb", "maintenance_database": "postgres", "psql": "",
            "wsl_distribution": "", "wsl_user": ""}


def database_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value):
        raise ValueError("Use a database name of 1-63 lowercase letters, digits or underscores, starting with a letter.")
    return value


def add_connection_arguments(parser):
    parser.add_argument("--config", type=Path, help=f"Connection config; default {CONFIG.name} if present")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--user")
    parser.add_argument("--database")
    parser.add_argument("--maintenance-database")
    parser.add_argument("--psql", help="Path to the installed psql client")
    parser.add_argument("--wsl-distribution", help="Use psql inside this existing WSL distribution")
    parser.add_argument("--wsl-user", help="Linux user for the WSL client process")
    parser.add_argument("--role", help="SET ROLE for each connection; membership must already be granted")
    parser.add_argument("--password", action="store_true", help="Prompt privately; do not put a password in command arguments")


def find_psql(explicit):
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_file():
            raise ValueError(f"psql client not found: {candidate}")
        return candidate.resolve()
    located = shutil.which("psql")
    if located:
        candidate = Path(located).resolve()
        if not candidate.is_relative_to(ROOT / ".runtime"):
            return candidate
    if os.name == "nt":
        installation = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PostgreSQL"
        candidates = list(installation.glob("*/bin/psql.exe"))
        candidates.sort(key=lambda p: int(p.parents[1].name) if p.parents[1].name.isdigit() else 0, reverse=True)
        if candidates:
            return candidates[0]
    raise ValueError("Installed psql client not found. Set psql in connection.local.json or use --psql. The old .runtime server is not used.")


class Connection:
    def __init__(self, args):
        self.role = getattr(args, "role", None)
        if self.role is not None:
            database_name(self.role)  # A strict SQL identifier, not SQL text.
        settings = dict(DEFAULTS)
        # Only the default config is optional; an explicit path must exist so a
        # typo cannot silently fall back to the default connection.
        config = getattr(args, "config", None)
        if config is not None and not config.is_file():
            raise ValueError(f"Connection config not found: {config}")
        config = config or CONFIG
        if config.is_file():
            configured = json.loads(config.read_text(encoding="utf-8-sig"))
            if not isinstance(configured, dict) or set(configured) - set(DEFAULTS):
                raise ValueError("Unknown connection config field; use connection.example.json and store passwords separately.")
            settings.update(configured)
        for key, variable in (("host", "PGHOST"), ("port", "PGPORT"), ("user", "PGUSER"),
                              ("database", "PGDATABASE"), ("psql", "AGENTDB_PSQL")):
            if os.environ.get(variable):
                settings[key] = os.environ[variable]
        for key in DEFAULTS:
            if getattr(args, key, None) is not None:
                settings[key] = getattr(args, key)
        unix_socket = isinstance(settings["host"], str) and settings["host"].startswith("/")
        if settings["host"] not in {"127.0.0.1", "localhost", "::1"} and not (
                unix_socket and (os.name != "nt" or settings["wsl_distribution"])):
            raise ValueError("Use a loopback host or a local Unix socket directory (native Linux / WSL).")
        settings["port"] = int(settings["port"])
        if not 1 <= settings["port"] <= 65535:
            raise ValueError("Invalid PostgreSQL port")
        if not isinstance(settings["user"], str) or not settings["user"].strip():
            raise ValueError("PostgreSQL user is required")
        database_name(settings["database"])
        database_name(settings["maintenance_database"])
        self.settings = settings
        self.env = dict(os.environ)
        # Explicit local connection parameters must not be redirected by an
        # unrelated libpq host address or service profile inherited from a shell.
        for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"):
            self.env.pop(key, None)
        self.env.update(PGCONNECT_TIMEOUT="5", PGCLIENTENCODING="UTF8", PGAPPNAME="agentdb")
        if args.password:
            self.env["PGPASSWORD"] = getpass.getpass("PostgreSQL password (not saved): ")
        if settings["wsl_distribution"]:
            if os.name != "nt":
                raise ValueError("WSL transport is for Windows; use the native psql client on Linux")
            executable = shutil.which("wsl.exe")
            if not executable:
                raise ValueError("wsl.exe not found")
            self.prefix = [executable, "--distribution", settings["wsl_distribution"]]
            if settings["wsl_user"]:
                self.prefix += ["--user", settings["wsl_user"]]
            self.prefix += ["--exec", "/usr/bin/env", "-u", "PGHOSTADDR", "-u", "PGSERVICE",
                            "-u", "PGSERVICEFILE", "PGCONNECT_TIMEOUT=5", "PGCLIENTENCODING=UTF8",
                            "PGAPPNAME=agentdb", settings["psql"] or "/usr/bin/psql"]
            # WSLENV forwards secrets as environment variables, never as argv.
            forwarded = [x for x in self.env.get("WSLENV", "").split(":") if x]
            for key in ("PGPASSWORD", "PGPASSFILE"):
                if key in self.env and not any(x.split("/")[0] == key for x in forwarded):
                    forwarded.append(key)
            self.env["WSLENV"] = ":".join(forwarded)
        else:
            self.prefix = [str(find_psql(settings["psql"]))]

    def client_args(self, database, stop_on_error=True):
        args = self.prefix + ["-X", "-qAt", "--no-password",
                "-h", self.settings["host"], "-p", str(self.settings["port"]),
                "-U", self.settings["user"], "-d", database_name(database)]
        if stop_on_error:
            args += ["-v", "ON_ERROR_STOP=1"]
        return args + ["-v", "VERBOSITY=verbose"]

    def session(self, database=None):
        """A long-lived psql process; see Session."""
        return Session(self, database)

    def execute(self, statement, database=None, timeout=120):
        target = database_name(database or self.settings["database"])
        args = self.client_args(target)
        if self.role:
            statement = f'SET ROLE "{self.role}";\n' + statement
        # Send bytes: text-mode stdin turns "\n" into "\r\n" on Windows, which
        # would leak into function bodies and multi-line literals.
        result = subprocess.run(args, input=statement.encode("utf-8"), capture_output=True,
                                timeout=timeout, env=self.env, creationflags=FLAGS)
        result.stdout, result.stderr = (
            data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
            for data in (result.stdout, result.stderr))
        return result

    def sql(self, statement, database=None):
        result = self.execute(statement, database)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        return result.stdout.strip()

    def check(self):
        result = json.loads(self.sql("""
          SELECT json_build_object('server_version',current_setting('server_version'),
            'server_version_num',current_setting('server_version_num')::integer,
            'host',inet_server_addr(),'port',current_setting('port')::integer,
            'user',current_user,'database',current_database());
        """, self.settings["maintenance_database"]))
        if result["server_version_num"] < 160000:
            raise RuntimeError("AgentDB requires PostgreSQL 16 or later")
        result["configured_host"] = self.settings["host"]
        result["transport"] = "wsl" if self.settings["wsl_distribution"] else "native"
        if self.settings["wsl_distribution"]:
            result["wsl_distribution"] = self.settings["wsl_distribution"]
        return result

    def create_database(self, name):
        # Strict identifier validation also avoids libpq interpreting a database
        # name as a connection string and silently connecting somewhere else.
        name = database_name(name)
        self.sql(f'CREATE DATABASE "{name}";', self.settings["maintenance_database"])

    def install(self, database=None):
        target = database_name(database or self.settings["database"])
        occupied = self.sql("""
          SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname IN ('vc','agent','app'))
            OR EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
              WHERE n.nspname NOT IN ('pg_catalog','information_schema')
                AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%');
        """, target)
        if occupied != "f":
            raise RuntimeError(f"Database {target} is not empty; installation stopped without overwriting its objects.")
        # All schema files install in one transaction, including the shared roles.
        statements = []
        for filename in ("001_core.sql", "002_demo_app.sql", "003_manager_role.sql", "004_merge_batch.sql"):
            source = (ROOT / "sql" / filename).read_text(encoding="utf-8")
            source = re.sub(r"(?m)^BEGIN;\s*$|^COMMIT;\s*$", "", source)
            statements.append(source)
        self.sql("BEGIN;\n" + "\n".join(statements) + "\nCOMMIT;", target)

    def install_manager(self, database=None):
        """Add the restricted snapshot API to an existing installation."""
        target = database_name(database or self.settings["database"])
        source = (ROOT / "sql/003_manager_role.sql").read_text(encoding="utf-8")
        self.sql(source, target)

    def install_merge_batch(self, database=None):
        """Replace vc.merge in an existing installation with the batched version."""
        target = database_name(database or self.settings["database"])
        source = (ROOT / "sql/004_merge_batch.sql").read_text(encoding="utf-8")
        self.sql(source, target)


class Session:
    """One long-lived psql process that runs scripts like Connection.execute.

    Starting wsl.exe + psql and logging in costs hundreds of milliseconds per
    call; reusing one process makes a call cost about a pipe round trip.
    Each execute() keeps the per-process semantics callers rely on: the script
    stops at its first error (returncode 3, error text on stderr), an open
    transaction is rolled back, and server state (settings, role, temp tables,
    advisory locks, prepared statements) and psql variables are reset before
    the next call. Calls run one at a time; use one Session per thread.

    psql runs each script with \\i from a private (mode 0600) temporary file
    on the client side, so ON_ERROR_STOP ends only that script, not the
    process. The file is written through psql's own output and kept for the
    session's lifetime; it still holds the last script until close().
    """

    # The -qAt output format a fresh process starts with; a script may change it.
    OUTPUT_DEFAULTS = ("\\pset format unaligned\n\\pset tuples_only on\n\\pset expanded off\n"
                       "\\pset fieldsep '|'\n\\pset recordsep '\\n'\n\\pset null ''\n"
                       "\\pset numericlocale off\n\\pset title\n\\timing off\n")
    # psql updates these itself after every command.
    AUTOMATIC = {"ERROR", "SQLSTATE", "ROW_COUNT", "LAST_ERROR_MESSAGE", "LAST_ERROR_SQLSTATE",
                 "SHELL_ERROR", "SHELL_EXIT_CODE", "LASTOID"}

    def __init__(self, connection, database=None):
        self.connection = connection
        self.database = database_name(database or connection.settings["database"])
        self.process, self.script_path = None, None
        self.lock = threading.Lock()
        try:
            self._start(120)
        except BaseException:
            self.close()
            raise

    def _start(self, timeout):
        self.process = subprocess.Popen(
            self.connection.client_args(self.database, stop_on_error=False),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.connection.env, creationflags=FLAGS)
        self.lines = {"stdout": queue.Queue(), "stderr": queue.Queue()}
        for name in self.lines:
            threading.Thread(target=self._drain, args=(self.process, name), daemon=True).start()
        deadline = time.monotonic() + timeout
        if self.script_path:
            pass  # A restarted process reuses the file it was given before.
        elif self.connection.settings["wsl_distribution"]:
            # The file lives next to psql inside WSL; reading it through /mnt
            # would cost about 10 ms per call. mktemp creates it with mode 0600.
            marker = "__agentdb_path" + uuid.uuid4().hex
            self._send(f"\\! mktemp /tmp/agentdb-session-XXXXXXXXXX\n\\echo {marker}\n")
            created, _ = self._read_until("stdout", marker, deadline)
            self.script_path = created[-1].strip() if created else ""
        else:
            handle, path = tempfile.mkstemp(prefix="agentdb-session-", suffix=".sql")
            os.close(handle)
            self.script_path = Path(path).as_posix()
        self.baseline, self.cleanup = {}, ""
        result = self._call("SELECT 'agentdb-session-ready';", timeout)
        if result.returncode or result.stdout.strip() != "agentdb-session-ready":
            self._stop()
            raise RuntimeError("psql session could not run its script file " + str(self.script_path) +
                               ":\n" + result.stdout + result.stderr)
        self.baseline, self.cleanup = self.variables, ""

    def _send(self, text):
        self.process.stdin.write(text.encode("utf-8"))
        self.process.stdin.flush()

    def _drain(self, process, name):
        stream, lines = getattr(process, name), self.lines[name]
        for raw in iter(stream.readline, b""):
            lines.put(raw.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n"))
        lines.put(None)

    def _read_until(self, name, marker, deadline):
        collected = []
        while True:
            try:
                line = self.lines[name].get(timeout=max(deadline - time.monotonic(), 0))
            except queue.Empty:
                raise subprocess.TimeoutExpired(self.script_path, 0) from None
            if line is None:
                raise RuntimeError("psql session ended:\n" + "".join(collected))
            if line.startswith(marker):
                return collected, line[len(marker):].strip()
            collected.append(line)

    def _call(self, statement, timeout):
        if self.connection.role:
            statement = f'SET ROLE "{self.connection.role}";\n' + statement
        # The trailing ";" sends an unterminated last statement, as psql does
        # at end of input. Hex keeps every byte exact (see Connection.execute
        # on "\r\n"); unaligned tuples-only output writes the text unchanged.
        script = ("\\set ON_ERROR_STOP on\n" + statement + "\n;\n\\set agentdb_session_done 1").encode("utf-8")
        done, listing, end = ("__agentdb_" + kind + uuid.uuid4().hex for kind in ("done", "vars", "end"))
        path = self.script_path.replace("'", "''")
        protocol = (self.cleanup + "\\unset agentdb_session_done\n" + self.OUTPUT_DEFAULTS +
                    f"\\o '{path}'\nSELECT convert_from(decode('{script.hex()}','hex'),'UTF8');\n\\o\n"
                    f"\\i '{path}'\n\\set ON_ERROR_STOP off\n\\o\n"
                    f"\\echo {done} :{{?agentdb_session_done}}\n\\warn {done}\n"
                    # What a process exit would do: end the transaction, drop session state.
                    "ROLLBACK;\nDISCARD ALL;\n"
                    f"\\echo {listing}\n\\set\n\\echo {end}\n\\warn {end}\n")
        deadline = time.monotonic() + timeout
        try:
            self._send(protocol)
            stdout, finished = self._read_until("stdout", done, deadline)
            self._read_until("stdout", listing, deadline)
            variables, _ = self._read_until("stdout", end, deadline)
            stderr, _ = self._read_until("stderr", done, deadline)
            reset, _ = self._read_until("stderr", end, deadline)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            self._stop()
            raise
        if any("ERROR:" in line for line in reset):
            self._stop()  # The reset itself failed; never reuse unknown state.
        self.variables = {}
        for line in variables:
            match = re.match(r"([A-Za-z0-9_]+) = '(.*)'$", line.rstrip("\n"))
            if match:
                self.variables[match.group(1)] = match.group(2)
        # Applied at the start of the next call: drop new variables, restore changed ones.
        self.cleanup = "".join(f"\\unset {name}\n" for name in self.variables if name not in self.baseline)
        self.cleanup += "".join(
            "\\set {} '{}'\n".format(name, value.replace("\\", "\\\\").replace("'", "''"))
            for name, value in self.baseline.items()
            if self.variables.get(name) != value and name not in self.AUTOMATIC)
        # psql names the included file in its messages; stdin input has no such prefix.
        prefix = re.compile("^psql:" + re.escape(self.script_path) + r":\d+: ")
        return subprocess.CompletedProcess(self.script_path, 0 if finished == "TRUE" else 3,
                                           "".join(stdout), "".join(prefix.sub("", line) for line in stderr))

    def _stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=30)
        self.process = None

    def execute(self, statement, database=None, timeout=120):
        if database is not None and database_name(database) != self.database:
            raise ValueError(f"This session is connected to {self.database}, not {database}")
        with self.lock:
            if self.process is None:
                # The previous process ended (timeout, crash or failed reset).
                # Start a new one; the failed call itself is never retried.
                self._start(timeout)
            return self._call(statement, timeout)

    def sql(self, statement, database=None):
        result = self.execute(statement, database)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        return result.stdout.strip()

    def close(self):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                try:
                    if self.script_path and self.connection.settings["wsl_distribution"]:
                        self._send(f"\\! rm -f '{self.script_path}'\n")  # mktemp name, no quotes
                    self.process.stdin.write(b"\\q\n")
                    self.process.stdin.close()
                    self.process.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self._stop()
            if self.script_path and not self.connection.settings["wsl_distribution"]:
                Path(self.script_path).unlink(missing_ok=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "install", "install-manager", "install-merge-batch"))
    add_connection_arguments(parser)
    args = parser.parse_args()
    try:
        connection = Connection(args)
        details = connection.check()
        if args.action == "install":
            target = connection.settings["database"]
            if target in {"postgres", "template0", "template1"}:
                raise ValueError("Install into a dedicated project database, such as agentdb")
            exists = connection.sql(f"SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname='{target}');",
                                    connection.settings["maintenance_database"])
            if exists == "f":
                connection.create_database(target)
            connection.install(target)
            details["project_database"] = target
            details["installed"] = True
        elif args.action == "install-manager":
            connection.install_manager()
            details["manager_installed_in"] = connection.settings["database"]
        elif args.action == "install-merge-batch":
            connection.install_merge_batch()
            details["merge_batch_installed_in"] = connection.settings["database"]
        print(json.dumps(details, indent=2))
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
