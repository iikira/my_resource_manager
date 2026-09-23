#!/usr/bin/env python3
"""Connect to a remote SSH server through an HTTP CONNECT proxy using paramiko,
then query localhost MySQL for version / databases / tables info.

NO credentials or endpoints are hardcoded — everything is read from env vars:
  SSH_USER      (default opc)
  DEST_HOST     REQUIRED
  DEST_PORT     (default 22)
  SSH_KEY_FILE  REQUIRED (path to private key)
  KEY_PASS      (key passphrase; may be empty)
  PROXY_HOST    REQUIRED
  PROXY_PORT    (default 8080)
  PROXY_USER
  PROXY_PASS
  MYSQL_USER    (default root)
  MYSQL_PASS    REQUIRED
  MYSQL_HOST    (default localhost)
"""
import os
import sys
import socket
import base64
import paramiko


def env(k, d=None):
    return os.environ.get(k, d)


def open_proxy_tunnel(dest_host, dest_port):
    ph = env("PROXY_HOST")
    if not ph:
        sys.exit("[!] PROXY_HOST env var required")
    pp = int(env("PROXY_PORT", "8080"))
    pu = env("PROXY_USER", "")
    ppwd = env("PROXY_PASS", "")
    s = socket.create_connection((ph, pp), timeout=30)
    s.settimeout(30)
    auth = base64.b64encode(f"{pu}:{ppwd}".encode()).decode()
    req = (
        f"CONNECT {dest_host}:{dest_port} HTTP/1.1\r\n"
        f"Host: {dest_host}:{dest_port}\r\n"
        f"Proxy-Authorization: Basic {auth}\r\n"
        f"Proxy-Connection: keep-alive\r\n"
        f"User-Agent: paramiko-tunnel/1.0\r\n\r\n"
    ).encode()
    s.sendall(req)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise ConnectionError("proxy closed before CONNECT response")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].decode("latin-1")
    if " 200 " not in status:
        raise ConnectionError(f"CONNECT rejected: {status}")
    s.settimeout(None)
    return s, rest


def run_remote(transport, cmd, timeout=30):
    chan = transport.open_session()
    chan.exec_command(cmd)
    chan.settimeout(timeout)
    out = b""
    err = b""
    try:
        while True:
            data = chan.recv(4096)
            if not data:
                break
            out += data
    except socket.timeout:
        pass
    try:
        while True:
            data = chan.recv_stderr(4096)
            if not data:
                break
            err += data
    except socket.timeout:
        pass
    rc = chan.recv_exit_status()
    chan.close()
    return out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), rc


def inspect_mysql(transport):
    mysql_user = env("MYSQL_USER", "root")
    mysql_pass = env("MYSQL_PASS", "")
    mysql_host = env("MYSQL_HOST", "localhost")
    if not mysql_pass:
        sys.exit("[!] MYSQL_PASS env var required")
    MYSQL = f"mysql -u{mysql_user} -p'{mysql_pass}' -h{mysql_host}"

    # 0) check mysql client presence
    o, e, rc = run_remote(transport, "which mysql mysqladmin 2>&1; echo EXIT=$?")
    print("=== mysql client ===")
    print(o)
    if "EXIT=0" not in o and "/mysql" not in o:
        print("[!] no mysql client found; trying via docker/which mysqld")
        o2, _, _ = run_remote(
            transport,
            "sudo -n which mysqld mariadbd 2>&1; systemctl status mariadb mysqld 2>&1 | head -5; docker ps 2>&1 | head -5",
        )
        print(o2)
        return

    for title, q in (
        ("VERSION", "SELECT VERSION();"),
        ("CURRENT_USER", "SELECT CURRENT_USER();"),
        ("DATABASES", "SHOW DATABASES;"),
    ):
        o, e, rc = run_remote(transport, f"{MYSQL} -N -B -e \"{q}\" 2>&1")
        print(f"=== {title} ===")
        print(o)

    # list tables per database (skip system schemas)
    o, e, rc = run_remote(
        transport,
        f"{MYSQL} -N -B -e \"SELECT schema_name FROM information_schema.schemata ORDER BY schema_name;\" 2>&1",
    )
    dbs = [
        ln.strip()
        for ln in o.splitlines()
        if ln.strip()
        and ln.strip() not in ("information_schema", "performance_schema", "mysql", "sys")
    ]
    print("=== user databases ===")
    print("\n".join(dbs) if dbs else "(none)")

    for db in dbs:
        o, e, rc = run_remote(
            transport,
            f"{MYSQL} -N -B -e \"SELECT table_name, table_rows, ROUND(data_length/1024/1024,2) AS data_mb, engine FROM information_schema.tables WHERE table_schema='{db}' ORDER BY table_name;\" 2>&1",
        )
        print(f"=== tables in `{db}` ===")
        print(o if o.strip() else "(no tables)")


def main():
    dest_host = env("DEST_HOST")
    if not dest_host:
        sys.exit("[!] DEST_HOST env var required")
    dest_port = int(env("DEST_PORT", "22"))
    user = env("SSH_USER", "opc")
    key_file = env("SSH_KEY_FILE")
    key_pass = env("KEY_PASS", "") or None
    if not key_file:
        sys.exit("[!] SSH_KEY_FILE env var required")

    print(f"[*] opening tunnel via proxy -> {dest_host}:{dest_port}")
    sock, rest = open_proxy_tunnel(dest_host, dest_port)
    print(f"[+] tunnel established (leftover={len(rest)}B)")

    transport = paramiko.Transport(sock)
    transport.connect()

    key = None
    for cls, name in (
        (paramiko.Ed25519Key, "Ed25519"),
        (paramiko.RSAKey, "RSA"),
        (paramiko.ECDSAKey, "ECDSA"),
    ):
        try:
            key = cls.from_private_key_file(key_file, password=key_pass)
            print(f"[+] loaded {name} key")
            break
        except Exception:
            pass
    if key is None:
        sys.exit("[!] failed to load private key")

    transport.auth_publickey(user, key)
    if not transport.is_authenticated():
        sys.exit(f"[!] authentication failed for {user}")
    print(f"[+] authenticated as {user}")

    inspect_mysql(transport)

    transport.close()


if __name__ == "__main__":
    main()
