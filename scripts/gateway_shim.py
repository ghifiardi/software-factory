#!/usr/bin/env python3
"""Local shim between Claude Code and an Anthropic-compatible gateway.

Bifrost v2.2.4's /anthropic route keeps only the FIRST system block and drops
the rest, but Claude Code sends several (the instructions, working directory and
tool rules live in the later ones). This shim merges all system blocks into one
before forwarding. Everything else passes through unchanged, including the
streamed response.

  GATEWAY_URL=https://host/anthropic GATEWAY_KEY=... python3 gateway_shim.py [port]
  then: ANTHROPIC_BASE_URL=http://127.0.0.1:<port>  ANTHROPIC_AUTH_TOKEN=<anything>

The real key comes only from GATEWAY_KEY and is sent as a Bearer token; the
token Claude Code sends is discarded. Listens on 127.0.0.1 only. Stdlib only.
"""
import http.client
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = urllib.parse.urlsplit(os.environ["GATEWAY_URL"].rstrip("/"))
KEY = os.environ["GATEWAY_KEY"]
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
       "proxy-authorization", "proxy-authenticate", "host", "content-length",
       "authorization", "x-api-key"}


def merge_system(body: dict) -> dict:
    """Collapse a list of system text blocks into one block (keeps cache_control off)."""
    system = body.get("system")
    if isinstance(system, list) and len(system) > 1:
        text = "\n\n".join(b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text")
        body["system"] = [{"type": "text", "text": text}]
    return body


class Shim(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep stdout quiet; errors still go to stderr
        pass

    def _forward(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        if raw and self.path.split("?")[0].endswith("/messages"):
            try:
                raw = json.dumps(merge_system(json.loads(raw))).encode()
            except ValueError:
                pass
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        headers["Authorization"] = f"Bearer {KEY}"
        headers["Content-Length"] = str(len(raw))
        conn_cls = http.client.HTTPSConnection if UPSTREAM.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(UPSTREAM.netloc, timeout=900)
        try:
            conn.request(self.command, UPSTREAM.path + self.path, body=raw or None, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in HOP:
                    self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        except Exception as e:  # upstream unreachable: tell the client, don't hang
            print(f"gateway_shim: {e.__class__.__name__}: {e}", file=sys.stderr)
            msg = json.dumps({"type": "error", "error": {"type": "api_error", "message": "gateway unreachable"}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(msg)
        finally:
            conn.close()
            self.close_connection = True

    do_POST = do_GET = _forward


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    ThreadingHTTPServer(("127.0.0.1", port), Shim).serve_forever()
