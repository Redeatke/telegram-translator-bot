#!/usr/bin/env python3
"""
Lightweight HTTP CONNECT proxy for routing yt-dlp traffic through your home IP.

Usage:
  1. Run this script on your home PC:
       python home_proxy.py

  2. Expose it via ngrok (free):
       ngrok tcp 8899

  3. Copy the ngrok forwarding address (e.g. tcp://0.tcp.us.ngrok.io:12345)
     and set it as YOUTUBE_PROXY in your Render environment:
       YOUTUBE_PROXY=http://0.tcp.us.ngrok.io:12345

The bot's yt-dlp will then route YouTube downloads through your residential IP,
bypassing YouTube's datacenter IP blocks.
"""

import socket
import threading
import select
import logging
import sys
import os

# ─── Configuration ────────────────────────────────────────────────────────────

HOST = "0.0.0.0"
PORT = int(os.getenv("PROXY_PORT", "8899"))
BUFFER_SIZE = 65536
AUTH_TOKEN = os.getenv("PROXY_AUTH", "")  # Optional: set to require auth

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("home_proxy")


def pipe(sock_a, sock_b):
    """Bidirectionally copy data between two sockets until one closes."""
    try:
        while True:
            readable, _, _ = select.select([sock_a, sock_b], [], [], 60)
            if not readable:
                break  # Timeout — connection idle
            for sock in readable:
                data = sock.recv(BUFFER_SIZE)
                if not data:
                    return
                target = sock_b if sock is sock_a else sock_a
                target.sendall(data)
    except (OSError, BrokenPipeError, ConnectionResetError):
        pass


def handle_client(client_sock, client_addr):
    """Handle one incoming proxy request."""
    try:
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = client_sock.recv(4096)
            if not chunk:
                return
            request += chunk

        first_line = request.split(b"\r\n")[0].decode("utf-8", errors="replace")
        parts = first_line.split()
        if len(parts) < 3:
            client_sock.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return

        method = parts[0].upper()

        # Optional auth check
        if AUTH_TOKEN:
            auth_ok = False
            for line in request.split(b"\r\n"):
                if line.lower().startswith(b"proxy-authorization:"):
                    token = line.split(b":", 1)[1].strip().decode()
                    if token == f"Basic {AUTH_TOKEN}" or token == AUTH_TOKEN:
                        auth_ok = True
                        break
            if not auth_ok:
                client_sock.sendall(
                    b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                    b"Proxy-Authenticate: Basic realm=\"proxy\"\r\n\r\n"
                )
                return

        if method == "CONNECT":
            # HTTPS tunneling — this is what yt-dlp uses
            host_port = parts[1].decode()
            if ":" in host_port:
                host, port = host_port.rsplit(":", 1)
                port = int(port)
            else:
                host, port = host_port, 443

            logger.info(f"CONNECT {host}:{port} from {client_addr[0]}")

            try:
                remote_sock = socket.create_connection((host, port), timeout=15)
            except Exception as e:
                logger.warning(f"Failed to connect to {host}:{port}: {e}")
                client_sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                return

            client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            pipe(client_sock, remote_sock)
            remote_sock.close()

        else:
            # Plain HTTP forwarding (GET, POST, etc.)
            url = parts[1].decode()
            if url.startswith("http://"):
                url = url[7:]
                slash = url.find("/")
                if slash == -1:
                    host_port, path = url, "/"
                else:
                    host_port, path = url[:slash], url[slash:]
                if ":" in host_port:
                    host, port = host_port.rsplit(":", 1)
                    port = int(port)
                else:
                    host, port = host_port, 80

                logger.info(f"{method} {host}:{port}{path} from {client_addr[0]}")

                try:
                    remote_sock = socket.create_connection((host, port), timeout=15)
                except Exception as e:
                    logger.warning(f"Failed to connect to {host}:{port}: {e}")
                    client_sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    return

                # Rewrite request to be a direct request (not proxy-style)
                rewritten = f"{method} {path} HTTP/1.1\r\n".encode()
                header_lines = request.split(b"\r\n")[1:]
                rewritten += b"\r\n".join(header_lines)
                remote_sock.sendall(rewritten)
                pipe(client_sock, remote_sock)
                remote_sock.close()
            else:
                client_sock.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")

    except Exception as e:
        logger.error(f"Error handling {client_addr}: {e}")
    finally:
        try:
            client_sock.close()
        except Exception:
            pass


def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(50)

    logger.info(f"")
    logger.info(f"  🏠 Home Proxy running on {HOST}:{PORT}")
    logger.info(f"")
    logger.info(f"  Next steps:")
    logger.info(f"    1. Open another terminal and run:")
    logger.info(f"       ngrok tcp {PORT}")
    logger.info(f"    2. Copy the Forwarding address from ngrok")
    logger.info(f"    3. Set YOUTUBE_PROXY in Render dashboard:")
    logger.info(f"       YOUTUBE_PROXY=http://<ngrok-address>:<port>")
    logger.info(f"")

    try:
        while True:
            client_sock, client_addr = server.accept()
            thread = threading.Thread(
                target=handle_client,
                args=(client_sock, client_addr),
                daemon=True,
            )
            thread.start()
    except KeyboardInterrupt:
        logger.info("Shutting down proxy...")
        server.close()


if __name__ == "__main__":
    main()
