#!/usr/bin/env python3
"""Load a .env file into os.environ then run the remote integration test.

Usage:
    python run_test.py /path/to/secrets/.env

The .env file path is passed as an argument (no credentials on the command line).
Values containing shell-special chars are expected to be quoted in the .env.
"""
import os
import sys


def load_env(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:]
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            # strip surrounding quotes if present
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            os.environ[k] = v


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: run_test.py <path-to-.env>")
    env_file = sys.argv[1]
    load_env(env_file)
    # Append the tests dir (this file's dir) to import and run the test.
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    import remote_integration  # noqa: E402
    remote_integration.main()
