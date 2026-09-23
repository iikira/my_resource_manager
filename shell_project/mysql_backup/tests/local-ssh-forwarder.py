#!/usr/bin/env python3
"""Local TCP forwarder that tunnels a local port through an HTTP CONNECT proxy
to a remote host:port, so ssh can connect to 127.0.0.1:<local_port> WITHOUT
using a ProxyCommand (avoids Windows OpenSSH ProxyCommand stdout-pipe quirks).

Usage:
  local-ssh-forwarder.py <local_port> <dest_host> <dest_port>
Env: PROXY_HOST, PROXY_PORT, PROXY_USER, PROXY_PASS

Then:  ssh -p <local_port> syy@127.0.0.1 ...
"""
import os
import sys
import socket
import base64
import threading
import select

LOCAL_PORT = int(sys.argv[1])
DEST_HOST = sys.argv[2]
DEST_PORT = int(sys.argv[3])
PROXY_HOST = os.environ["PROXY_HOST"]
PROXY_PORT = int(os.environ["PROXY_PORT"])
PROXY_USER = os.environ.get("PROXY_USER", "")
PROXY_PASS = os.environ.get("PROXY_PASS", "")


def open_tunnel():
    s = socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=30)
    s.settimeout(30)
    auth = base64.b64encode(f"{PROXY_USER}:{PROXY_PASS}".encode()).decode()
    req = (
        f"CONNECT {DEST_HOST}:{DEST_PORT} HTTP/1.1\r\n"
        f"Host: {DEST_HOST}:{DEST_PORT}\r\n"
        f"Proxy-Authorization: Basic {auth}\r\n"
        f"Proxy-Connection: keep-alive\r\n"
        f"User-Agent: ssh-forwarder/1.0\r\n\r\n"
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
    # Any leftover bytes are part of the tunnel stream; keep them.
    return s, rest


def splice(a, b, rest_for_b=b""):
    """Forward a<->b until either closes. Optionally write leftover to b first."""
    try:
        if rest_for_b:
            b.sendall(rest_for_b)
    except OSError:
        return
    socks = [a, b]
    try:
        while True:
            r, _, x = select.select(socks, [], socks, 60)
            if x:
                break
            if not r:
                # idle; loop again
                continue
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                other = b if s is a else a
                try:
                    other.sendall(data)
                except OSError:
                    return
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(client):
    try:
        tun, rest = open_tunnel()
    except Exception as e:
        sys.stderr.write(f"forwarder: tunnel failed: {e!r}\n")
        sys.stderr.flush()
        try:
            client.close()
        except OSError:
            pass
        return
    try:
        splice(client, tun, rest)
    finally:
        for s in (client, tun):
            try:
                s.close()
            except OSError:
                pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", LOCAL_PORT))
    srv.listen(8)
    sys.stderr.write(f"forwarder: listening on 127.0.0.1:{LOCAL_PORT} -> {DEST_HOST}:{DEST_PORT} via {PROXY_HOST}:{PROXY_PORT}\n")
    sys.stderr.flush()
    while True:
        try:
            client, _ = srv.accept()
        except OSError:
            break
        t = threading.Thread(target=handle, args=(client,), daemon=True)
        t.start()


if __name__ == "__main__":
    main()
