"""Ephemeral SMTP sink requiring STARTTLS for Nexora CI integration tests."""
from __future__ import annotations
import argparse
import json
import ssl
import time
from pathlib import Path
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult

class Handler:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
    async def handle_DATA(self, server, session, envelope):
        raw = envelope.content.decode("utf-8", "replace") if isinstance(envelope.content, bytes) else str(envelope.content)
        with self.path.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"mail_from": envelope.mail_from, "rcpt_tos": envelope.rcpt_tos, "data": raw}) + "\n")
        return "250 Message accepted by isolated Nexora inbox"

def authenticate(server, session, envelope, mechanism, auth_data):
    return AuthResult(success=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2525)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--outbox", required=True)
    args = parser.parse_args()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)
    server = Controller(Handler(Path(args.outbox)), hostname=args.host, port=args.port,
                        tls_context=context, require_starttls=True, authenticator=authenticate)
    server.start()
    print(f"Isolated STARTTLS inbox listening on {args.host}:{args.port}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()

if __name__ == "__main__":
    main()
