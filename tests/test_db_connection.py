"""Connection settings checks in db.py; no PostgreSQL server or psql needed."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import db


class ConnectionSettingsTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name) / "connection.json"
        self.config.write_text("{}", encoding="utf-8")
        environment = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
        patches = [mock.patch.dict(os.environ, environment, clear=True),
                   mock.patch("db.find_psql", return_value=Path("psql")),
                   mock.patch("db.shutil.which", return_value="wsl.exe")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def connect(self, *argv, config=None):
        if config is not None:
            self.config.write_text(json.dumps(config), encoding="utf-8")
        parser = argparse.ArgumentParser()
        db.add_connection_arguments(parser)
        return db.Connection(parser.parse_args(["--config", str(self.config), *argv]))

    def test_loopback_without_tls_settings_is_unchanged(self):
        connection = self.connect()
        self.assertNotIn("PGSSLMODE", connection.env)
        self.assertNotIn("PGSSLROOTCERT", connection.env)
        self.assertEqual(connection.client_args("agentdb")[connection.client_args("agentdb").index("-h") + 1],
                         "127.0.0.1")

    def test_remote_host_requires_verify_full(self):
        for argv in ([], ["--sslmode", "require"], ["--sslmode", "verify-ca", "--sslrootcert", "/ca.pem"],
                     ["--sslmode", "prefer"]):
            with self.subTest(argv=argv), self.assertRaisesRegex(ValueError, "verify-full"):
                self.connect("--host", "db.staging.example", *argv)

    def test_verify_modes_need_a_ca_certificate(self):
        for mode in ("verify-ca", "verify-full"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "sslrootcert"):
                self.connect("--host", "db.staging.example", "--sslmode", mode)

    def test_remote_host_with_verify_full_sets_libpq_tls(self):
        connection = self.connect("--host", "db.staging.example", "--sslmode", "verify-full",
                                  "--sslrootcert", "/etc/agentdb/staging-ca.pem")
        self.assertEqual(connection.env["PGSSLMODE"], "verify-full")
        self.assertEqual(connection.env["PGSSLROOTCERT"], "/etc/agentdb/staging-ca.pem")
        args = connection.client_args("agentdb")
        self.assertEqual(args[args.index("-h") + 1], "db.staging.example")

    def test_ip_addresses_are_accepted_as_remote_hosts(self):
        for host in ("10.0.0.5", "fd00::5"):
            with self.subTest(host=host):
                self.connect("--host", host, "--sslmode", "verify-full", "--sslrootcert", "/ca.pem")

    def test_host_lists_and_connection_strings_are_rejected(self):
        for host in ("a.example,b.example", "host=a.example", "postgresql://a.example/db", "a example", "-h"):
            with self.subTest(host=host), self.assertRaisesRegex(ValueError, "single host"):
                self.connect(f"--host={host}", "--sslmode", "verify-full", "--sslrootcert", "/ca.pem")

    def test_inherited_tls_environment_is_ignored(self):
        with mock.patch.dict(os.environ, {"PGSSLMODE": "disable", "PGSSLROOTCERT": "/tmp/other.pem",
                                          "PGREQUIRESSL": "0"}):
            plain = self.connect()
            remote = self.connect("--host", "db.staging.example", "--sslmode", "verify-full",
                                  "--sslrootcert", "/ca.pem")
        self.assertFalse(set(db.SSL_ENVIRONMENT) & set(plain.env))
        self.assertEqual((remote.env["PGSSLMODE"], remote.env["PGSSLROOTCERT"]), ("verify-full", "/ca.pem"))
        self.assertNotIn("PGREQUIRESSL", remote.env)

    def test_remote_host_from_environment_needs_tls_too(self):
        with mock.patch.dict(os.environ, {"PGHOST": "db.staging.example"}), \
                self.assertRaisesRegex(ValueError, "verify-full"):
            self.connect()

    def test_tls_settings_from_config_file(self):
        connection = self.connect(config={"host": "db.staging.example", "sslmode": "verify-full",
                                          "sslrootcert": "/ca.pem"})
        self.assertEqual(connection.env["PGSSLMODE"], "verify-full")
        with self.assertRaisesRegex(ValueError, "must be a string"):
            self.connect(config={"sslmode": True})
        with self.assertRaisesRegex(ValueError, "sslmode must be one of"):
            self.connect(config={"sslmode": "verify_full"})

    @unittest.skipUnless(os.name == "nt", "the WSL transport is Windows-only")
    def test_wsl_transport_passes_tls_inside_the_distribution(self):
        connection = self.connect("--wsl-distribution", "Ubuntu", "--host", "db.staging.example",
                                  "--sslmode", "verify-full", "--sslrootcert", "/etc/agentdb/ca.pem")
        prefix = connection.prefix
        psql = prefix.index("/usr/bin/psql")
        self.assertIn("PGSSLMODE=verify-full", prefix[:psql])
        self.assertIn("PGSSLROOTCERT=/etc/agentdb/ca.pem", prefix[:psql])
        for key in db.SSL_ENVIRONMENT:
            self.assertIn(key, prefix[prefix.index("-u"):psql])


if __name__ == "__main__":
    unittest.main()
