#!/usr/bin/env python3
"""HTTP CONNECT tunnel for OpenSSH ProxyCommand.

Usage: proxy-connect.py <dest_host> <dest_port>
Env: PROXY_HOST, PROXY_PORT, PROXY_USER, PROXY_PASS

Establishes an HTTP CONNECT tunnel through the proxy, then splices
stdin<->socket<->stdout using raw fd I/O in two threads.
"""
import os
import sys
import socket
import base64
import threading

DEST_HOST = sys.argv[1]
DEST_PORT = int(sys.argv[2])
PROXY_HOST = os.environ["PROXY_HOST"]
PROXY_PORT = int(os.environ["PROXY_PORT"])
PROXY_USER = os.environ.get("PROXY_USER", "")
PROXY_PASS = os.environ.get("PROXY_PASS", "")


def main():
    sock = socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=30)
    sock.settimeout(30)
    auth = base64.b64encode(f"{PROXY_USER}:{PROXY_PASS}".encode()).decode()
    req = (
        f"CONNECT {DEST_HOST}:{DEST_PORT} HTTP/1.1\r\n"
        f"Host: {DEST_HOST}:{DEST_PORT}\r\n"
        f"Proxy-Authorization: Basic {auth}\r\n"
        f"Proxy-Connection: keep-alive\r\n"
        f"User-Agent: ssh-proxy/1.0\r\n\r\n"
    ).encode()
    sock.sendall(req)

    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            sys.stderr.write("proxy: closed before CONNECT response\n")
            sys.exit(2)
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].decode("latin-1")
    if " 200 " not in status:
        sys.stderr.write(f"proxy: CONNECT rejected: {status}\n")
        sys.exit(3)
    sys.stderr.write(f"proxy: CONNECT 200 OK -> {DEST_HOST}:{DEST_PORT}\n")
    sock.settimeout(None)

    out_fd = sys.stdout.fileno()
    sock_fd = sock.fileno()

    # Flush any leftover bytes from the CONNECT response into the tunnel stream.
    if rest:
        os.write(out_fd, rest)

    stop = threading.Event()

    def stdin_to_sock():
        try:
            while not stop.is_set():
                data = os.read(0, 65536)
                if not data:
                    sys.stderr.write("proxy: stdin EOF\n"); sys.stderr.flush()
                    break
                sock.sendall(data)
        except OSError as e:
            sys.stderr.write(f"proxy: stdin OSError {e!r}\n"); sys.stderr.flush()
        finally:
            stop.set()

    def sock_to_stdout():
        try:
            while not stop.is_set():
                data = sock.recv(65536)
                if not data:
                    sys.stderr.write("proxy: sock EOF\n"); sys.stderr.flush()
                    break
                # Write directly to the stdout fd; loop to handle partial writes.
                mv = memoryview(data)
                sent = 0
                while sent < len(mv):
                    n = os.write(out_fd, mv[sent:])
                    if n == 0:
                        break
                    sent += n
        except OSError as e:
            sys.stderr.write(f"proxy: sock OSError {e!r}\n"); sys.stderr.flush()
        finally:
            stop.set()

    t1 = threading.Thread(target=stdin_to_sock, daemon=True)
    t2 = threading.Thread(target=sock_to_stdout, daemon=True)
    t1.start()
    t2.start()
    sys.stderr.write("proxy: pumps started\n")
    sys.stderr.flush()
    stop.wait()
    sys.stderr.write("proxy: done, exiting\n")
    sys.stderr.flush()
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        sys.stderr.write(f"proxy: FATAL {e!r}\n{traceback.format_exc()}\n")
        sys.stderr.flush()
        sys.exit(1)
