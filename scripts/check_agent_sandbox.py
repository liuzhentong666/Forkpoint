"""Run inside the sandbox an agent will run in, as the agent would. Exit 1 if any way to more power is open.

No database rule can stop a process that can become the superuser, so an agent with a shell has to run
where it cannot: in a container, or under a separate OS account. This checks that one-time setup. It needs
only Python and psql, because it runs in the sandbox, not in this repository. With PGUSER, PGPASSWORD and
the like set (agent_boundary.py run sets them), it also checks that the login works and is not a superuser.
It also looks for credentials lying around, because agents have found and used them: names only, never values.
Give --allow-env for a variable the agent is meant to have, such as a model API key.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

results = []


def record(name, closed, detail):
    results.append(dict(name=name, status="HOLDS" if closed else "GAP", detail=detail))
    print(f"[{'HOLDS' if closed else '  GAP'}] {name}: {detail}", flush=True)


def runs(argv, environment=None, timeout=20):
    """Exit code, or None if the program is missing or could not run."""
    try:
        return subprocess.run(argv, env=environment, capture_output=True, timeout=timeout).returncode
    except (OSError, subprocess.TimeoutExpired):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--allow-env", action="append", default=[], help="a variable the agent is meant to have; repeat as needed")
    args = parser.parse_args()
    allowed = {"PGPASSWORD", *(name.upper() for name in args.allow_env)}
    names = sorted(name for name in os.environ if name.upper() not in allowed and
                   re.search(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|ACCESS_KEY|PRIVATE_KEY", name.upper()))
    record("no_other_credentials_in_the_environment", not names, f"found: {names}" if names else "none")
    home = Path.home()
    places = [home / ".pgpass", home / ".netrc", home / ".aws" / "credentials", home / ".kube" / "config", home / ".docker" / "config.json",
              home / ".config" / "gcloud", home / ".config" / "railway", home / ".railway", home / "AppData" / "Roaming" / "postgresql" / "pgpass.conf"]
    places += list((home / ".ssh").glob("id_*")) if (home / ".ssh").is_dir() else []
    for folder in (Path.cwd(), *Path.cwd().parents):
        places += [folder / name for name in ("connection.local.json", "connection.staging.local.json", ".env", ".env.local")]
    readable = [str(place) for place in places if place.exists()]
    record("no_credential_files_within_reach", not readable, f"found: {readable}" if readable else f"looked in {len(places)} places")

    # WSL's "--user postgres" reaches a peer-authenticated superuser with no password.
    routes = [shutil.which("wsl.exe"), shutil.which("wsl"), r"C:\Windows\System32\wsl.exe", "/mnt/c/Windows/System32/wsl.exe"]
    usable = [route for route in routes if route and Path(route).exists() and runs([route, "--status"]) == 0]
    record("cannot_run_wsl", not usable, f"runs: {usable}" if usable else "wsl.exe not found, or it does not run")

    # The Docker engine can start a privileged container, which reaches the host.
    docker = shutil.which("docker")
    reached = bool(docker and runs([docker, "version", "--format", "{{.Server.Version}}"]) == 0)
    socket = Path("/var/run/docker.sock").exists()
    record("cannot_reach_docker_engine", not reached and not socket,
           f"docker client reaches an engine: {reached}; socket mounted: {socket}")

    sudo = shutil.which("sudo")
    record("no_passwordless_sudo", not (sudo and runs([sudo, "-n", "true"]) == 0), "sudo -n true")

    psql = shutil.which("psql")
    if psql:
        # Only what needs no password of its own: a trust or peer rule, or a saved password.
        environment = {key: value for key, value in os.environ.items() if key != "PGPASSWORD"}
        port = os.environ.get("PGPORT", "5432")
        hosts = sorted({os.environ.get("PGHOST", "127.0.0.1"), "/var/run/postgresql", "127.0.0.1"})
        open_hosts = [host for host in hosts if runs(
            [psql, "-X", "-w", "-h", host, "-p", port, "-U", "postgres", "-d", "postgres", "-c", "SELECT 1"], environment) == 0]
        record("cannot_log_in_as_postgres_without_a_password", not open_hosts,
               f"logged in through: {open_hosts}" if open_hosts else f"refused on {hosts}")
    else:
        record("cannot_log_in_as_postgres_without_a_password", True, "no psql client here")

    if os.environ.get("PGUSER") and psql:
        probe = subprocess.run([psql, "-X", "-qAt", "-w", "-c",
                                "SELECT session_user||' superuser='||current_setting('is_superuser')||' '||vc.agent_status()"],
                               capture_output=True, text=True, timeout=20)
        record("own_login_works_and_is_not_a_superuser", probe.returncode == 0 and "superuser=off" in probe.stdout,
               (probe.stdout.strip() or probe.stderr.strip())[:300])

    gaps = [result["name"] for result in results if result["status"] == "GAP"]
    print(json.dumps(dict(checks=len(results), gaps=gaps)))
    sys.exit(1 if gaps else 0)


if __name__ == "__main__":
    main()
