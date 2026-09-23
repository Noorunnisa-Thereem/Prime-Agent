"""Shared PostgreSQL connection helper for the neurotwin_cpic_db scripts.

Credentials are never hardcoded and never requested interactively. Connection
parameters come from the standard libpq environment variables, with defaults
matching this project's known connection (host=localhost, port=5432,
database=cpic_db, user=postgres) for everything except the password, which
has no default and MUST be present in the environment as PGPASSWORD -- the
caller is expected to `export`/`set` it in their own shell before running
any script here.
"""

from __future__ import annotations

import os
import sys

import psycopg2


def get_connection():
    """Returns a new psycopg2 connection built entirely from environment
    variables. Raises a clear, actionable error (never a password prompt)
    if PGPASSWORD is not set."""
    password = os.environ.get("PGPASSWORD")
    if not password:
        print(
            "ERROR: PGPASSWORD is not set in the environment.\n"
            "Set it in your shell before running this script, e.g.:\n"
            "  PowerShell:  $env:PGPASSWORD = '...'\n"
            "  cmd.exe:     set PGPASSWORD=...\n"
            "  bash:        export PGPASSWORD=...\n"
            "This script never reads or stores the password any other way.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    return psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ.get("PGDATABASE", "cpic_db"),
        user=os.environ.get("PGUSER", "postgres"),
        password=password,
    )
