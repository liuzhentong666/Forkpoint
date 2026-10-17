"""Connect to an existing local PostgreSQL server; never manage its lifecycle."""
import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
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

    def execute(self, statement, database=None, timeout=120):
        target = database_name(database or self.settings["database"])
        args = self.prefix + ["-X", "-qAt", "--no-password",
                "-h", self.settings["host"], "-p", str(self.settings["port"]),
                "-U", self.settings["user"], "-d", target,
                "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=verbose"]
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
        for filename in ("001_core.sql", "002_demo_app.sql", "003_manager_role.sql"):
            source = (ROOT / "sql" / filename).read_text(encoding="utf-8")
            source = re.sub(r"(?m)^BEGIN;\s*$|^COMMIT;\s*$", "", source)
            statements.append(source)
        self.sql("BEGIN;\n" + "\n".join(statements) + "\nCOMMIT;", target)

    def install_manager(self, database=None):
        """Add the restricted snapshot API to an existing installation."""
        target = database_name(database or self.settings["database"])
        source = (ROOT / "sql/003_manager_role.sql").read_text(encoding="utf-8")
        self.sql(source, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "install", "install-manager"))
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
        print(json.dumps(details, indent=2))
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
