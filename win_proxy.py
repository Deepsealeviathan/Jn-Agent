#!/usr/bin/env python3
"""Windows-side TCP forwarder for the agent web service.

Default: 0.0.0.0:8000 -> 127.0.0.1:8010 (the WSL server).
"""

import argparse
import select
import socket
import socketserver


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            upstream = socket.create_connection(self.server.upstream, timeout=10)
        except OSError:
            return
        try:
            upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sockets = [self.request, upstream]
            while True:
                readable, _, _ = select.select(sockets, [], [], 600)
                if not readable:
                    break
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    target = upstream if source is self.request else self.request
                    target.sendall(data)
        except OSError:
            pass
        finally:
            try:
                upstream.close()
            except OSError:
                pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser(description="TCP forwarder to the WSL agent server")
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, default=8000)
    parser.add_argument("--upstream-host", default="127.0.0.1")
    parser.add_argument("--upstream-port", type=int, default=8010)
    args = parser.parse_args()

    with Server((args.listen_host, args.listen_port), Handler) as server:
        server.upstream = (args.upstream_host, args.upstream_port)
        print(
            f"forwarding {args.listen_host}:{args.listen_port} -> "
            f"{args.upstream_host}:{args.upstream_port}",
            flush=True,
        )
        server.serve_forever()


if __name__ == "__main__":
    main()
