#!/usr/bin/env python3
"""Remote integration test for the mysql-backup RPM.

Builds and installs the RPM on a remote host reachable only through an HTTP
CONNECT proxy, then verifies the full backup chain (dump -> zip -> rclone
upload) by manually triggering the service.

NO credentials or site-specific endpoints are hardcoded. Everything is read
from environment variables (hostnames included — set them explicitly):

  SSH target:
    SSH_USER      (default opc)
    DEST_HOST     REQUIRED (no default)
    DEST_PORT     (default 22)
    SSH_KEY_FILE  (path to private key)
    KEY_PASS      (key passphrase; may be empty)

  Proxy (HTTP CONNECT):
    PROXY_HOST    REQUIRED (no default)
    PROXY_PORT    (default 8080)
    PROXY_USER
    PROXY_PASS

  MySQL (for the live conf on the remote):
    MYSQL_USER    (default root)
    MYSQL_PASS    (real password; injected into the remote conf only)
    MYSQL_HOST    (default localhost)

  rclone:
    RCLONE_REMOTE (default drive:MySQLBackup) — used in the remote conf

  Optional:
    REMOTE_PROJECT_DIR  where to stage src/spec on the remote (default
                        /tmp/mysql-backup-test)
    KEEP                set to 1 to skip cleanup of staged files for inspection
"""
import os
import sys
import base64
import socket
import time

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
        f"User-Agent: mysql-backup-test/1.0\r\n\r\n"
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


def connect_ssh():
    dest_host = env("DEST_HOST")
    if not dest_host:
        sys.exit("[!] DEST_HOST env var required")
    dest_port = int(env("DEST_PORT", "22"))
    user = env("SSH_USER", "opc")
    key_file = env("SSH_KEY_FILE")
    key_pass = env("KEY_PASS", "") or None
    if not key_file:
        sys.exit("[!] SSH_KEY_FILE env var required")

    print(f"[*] tunnel via proxy -> {dest_host}:{dest_port}")
    sock, rest = open_proxy_tunnel(dest_host, dest_port)
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
        sys.exit(f"[!] auth failed for {user}")
    print(f"[+] authenticated as {user}")
    return transport


def run(transport, cmd, timeout=120):
    chan = transport.open_session()
    chan.exec_command(cmd)
    chan.settimeout(timeout)
    out = err = b""
    try:
        while True:
            d = chan.recv(4096)
            if not d:
                break
            out += d
    except socket.timeout:
        pass
    try:
        while True:
            d = chan.recv_stderr(4096)
            if not d:
                break
            err += d
    except socket.timeout:
        pass
    rc = chan.recv_exit_status()
    chan.close()
    return out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), rc


def sftp(transport):
    return paramiko.SFTPClient.from_transport(transport)


def put_text(transport, remote_path, content, mode=0o644):
    sf = sftp(transport)
    with sf.file(remote_path, "w") as f:
        f.write(content)
    sf.chmod(remote_path, mode)
    sf.close()


def put_local(transport, local_path, remote_path):
    sf = sftp(transport)
    sf.put(local_path, remote_path)
    sf.close()


HERE = os.path.dirname(os.path.abspath(__file__))
# The src/ tree is a sibling of tests/, i.e. <project>/src.
SRC_ROOT = os.path.normpath(os.path.join(HERE, "..", "src"))


def stage_files(transport, remote_root):
    run(transport, f"rm -rf {remote_root}; mkdir -p {remote_root}/src")
    # Walk the src tree and mirror it remotely via SFTP.
    sf = sftp(transport)
    created = set()

    def ensure(remote_dir):
        if remote_dir in created:
            return
        parts = remote_dir.strip("/").split("/")
        cur = ""
        for p in parts:
            cur += "/" + p
            try:
                sf.stat(cur)
            except IOError:
                sf.mkdir(cur)
        created.add(remote_dir)

    for dirpath, dirnames, filenames in os.walk(SRC_ROOT):
        rel = os.path.relpath(dirpath, SRC_ROOT)
        remote_dir = f"{remote_root}/src" + ("" if rel == "." else "/" + rel.replace(os.sep, "/"))
        ensure(remote_dir)
        for fn in filenames:
            local = os.path.join(dirpath, fn)
            remote = f"{remote_dir}/{fn}"
            sf.put(local, remote)
    # Spec. packaging/ is a sibling of tests/, so go up one level from HERE.
    spec_local = os.path.normpath(os.path.join(HERE, "..", "packaging", "mysql-backup.spec"))
    ensure(remote_root)
    sf.put(spec_local, f"{remote_root}/mysql-backup.spec")
    sf.close()
    run(transport, f"find {remote_root} -type f | head -50")


def main():
    transport = connect_ssh()

    # Baseline: who are we, what's installed.
    o, e, rc = run(transport, "id; uname -a; which rpm rpmbuild mysql mysqldump rclone zip 2>&1")
    print("=== baseline ===")
    print(o, e)

    remote_root = env("REMOTE_PROJECT_DIR", "/tmp/mysql-backup-test")

    print(f"[*] staging project to {remote_root}")
    stage_files(transport, remote_root)

    # Install build + runtime deps (best-effort across dnf/yum).
    print("[*] installing deps on remote")
    o, e, rc = run(transport,
                   "sudo sh -c 'if command -v dnf >/dev/null 2>&1; then "
                   "dnf install -y rpm-build rpmdevtools rclone mysql zip; "
                   "elif command -v yum >/dev/null 2>&1; then "
                   "yum install -y rpm-build rclone mysql zip; fi' 2>&1",
                   timeout=600)
    print(o, e)

    # Build the RPM on the remote.
    print("[*] building RPM on remote")
    build_script = f"""set -e
rpmdev-setuptree 2>/dev/null || mkdir -p ~/rpmbuild/{{SOURCES,SPECS,BUILD,RPMS,SRPMS}}
mkdir -p /tmp/stage/mysql-backup-1.0.0
cp -r {remote_root}/src /tmp/stage/mysql-backup-1.0.0/
cp {remote_root}/mysql-backup.spec /tmp/stage/mysql-backup-1.0.0/
cp /usr/share/licenses/systemd/LICENSE /tmp/stage/mysql-backup-1.0.0/LICENSE 2>/dev/null || echo dummy > /tmp/stage/mysql-backup-1.0.0/LICENSE
echo '# mysql-backup' > /tmp/stage/mysql-backup-1.0.0/README.md
cd /tmp/stage
tar czf ~/rpmbuild/SOURCES/mysql-backup-1.0.0.tar.gz mysql-backup-1.0.0
cp /tmp/stage/mysql-backup-1.0.0/mysql-backup.spec ~/rpmbuild/SPECS/
rpmbuild -bb ~/rpmbuild/SPECS/mysql-backup.spec
ls -l ~/rpmbuild/RPMS/noarch/ ~/rpmbuild/RPMS/*/*.rpm 2>/dev/null
"""
    o, e, rc = run(transport, f"bash -lc {shell_quote(build_script)}", timeout=600)
    print(o, e)
    if rc != 0:
        sys.exit("[!] RPM build failed on remote")

    # Locate the built rpm path.
    o, e, rc = run(transport, "ls ~/rpmbuild/RPMS/noarch/*.rpm ~/rpmbuild/RPMS/*/*.rpm 2>/dev/null | head -1")
    rpm_path = o.strip().splitlines()[-1].strip() if o.strip() else ""
    if not rpm_path:
        sys.exit("[!] could not locate built RPM")
    print(f"[+] built rpm: {rpm_path}")

    # Install the RPM.
    print("[*] installing RPM on remote")
    o, e, rc = run(transport, f"sudo rpm -Uvh --nodeps --force {shell_quote(rpm_path)} 2>&1", timeout=300)
    print(o, e)

    # Write the live config from env (real password injected here only, on the
    # remote host; never written to the repo).
    mysql_host = env("MYSQL_HOST", "localhost")
    mysql_user = env("MYSQL_USER", "root")
    mysql_pass = env("MYSQL_PASS", "")
    remote = env("RCLONE_REMOTE", "drive:MySQLBackup")
    backup_user = env("BACKUP_USER", "opc")
    if not mysql_pass:
        sys.exit("[!] MYSQL_PASS env var required (will be injected into remote conf only)")

    # Quote values for a sourced shell config; escape embedded double quotes.
    def q(v):
        return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'

    conf = f"""BACKUP_HOUR=5
BACKUP_USER={q(backup_user)}
MYSQL_HOST={q(mysql_host)}
MYSQL_USER={q(mysql_user)}
MYSQL_PASS={q(mysql_pass)}
MYSQL_EXCLUDE="information_schema performance_schema sys mysql"
RCLONE_REMOTE={q(remote)}
RCLONE_CONFIG=
BACKUP_DIR=/var/lib/mysql-backup
"""
    # Drop config in place with 0600 root-owned.
    put_text(transport, "/tmp/mb-conf", conf, 0o600)
    run(transport, "sudo cp /tmp/mb-conf /etc/mysql-backup/mysql-backup.conf && "
                   "sudo chmod 0600 /etc/mysql-backup/mysql-backup.conf && "
                   "sudo chown root:root /etc/mysql-backup/mysql-backup.conf")

    # Enable (generates service+timer, enables timer). Needs root; BACKUP_USER
    # must exist. Ensure the dir is usable by BACKUP_USER.
    print("[*] enabling mysql-backup")
    o, e, rc = run(transport,
                   f"sudo mkdir -p /var/lib/mysql-backup && sudo chown {backup_user}:{backup_user} /var/lib/mysql-backup && "
                   "sudo /usr/sbin/mysql-backup-enable 2>&1", timeout=120)
    print(o, e)

    # Manually trigger one backup run and capture journal.
    print("[*] triggering one backup run")
    o, e, rc = run(transport, "sudo systemctl start mysql-backup.service 2>&1; "
                              "sleep 5; sudo journalctl -u mysql-backup.service -n 50 --no-pager 2>&1",
                   timeout=300)
    print(o, e)

    # Check service exit status.
    o, e, rc = run(transport, "systemctl is-active mysql-backup.service 2>&1; "
                              "systemctl show mysql-backup.service -p ExecMainStatus -p Result --no-pager 2>&1")
    print("=== service status ===")
    print(o, e)

    # Verify the zip landed on the rclone remote. The service runs as
    # BACKUP_USER, so it uses that user's rclone config — verify the same way.
    print(f"[*] listing rclone remote {remote} as {backup_user}")
    o, e, rc = run(transport,
                   f"sudo -u {backup_user} rclone lsf {shell_quote(remote)} 2>&1 | tail -20",
                   timeout=300)
    print(o, e)

    if env("KEEP", "0") != "1":
        run(transport, f"sudo systemctl disable --now mysql-backup.timer 2>/dev/null; "
                       f"sudo rpm -e mysql-backup 2>&1; rm -rf {remote_root} /tmp/mb-conf")
        print("[*] cleanup done (set KEEP=1 to skip)")

    print("[done] integration test finished")


def shell_quote(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":
    main()
