"""STARTTLS SMTP inbox for the isolated Nexora browser run."""
from __future__ import annotations
import argparse
import json
import ssl
import time
from pathlib import Path
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult

class Inbox:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def handle_DATA(self, server, session, envelope):
        body = envelope.content
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        with self.path.open("a", encoding="utf-8") as target:
            target.write(json.dumps({
                "mail_from": envelope.mail_from,
                "rcpt_tos": envelope.rcpt_tos,
                "data": body,
            }, ensure_ascii=False) + "\n")
        return "250 Stored in Nexora's isolated walkthrough inbox"

def authenticate(server, session, envelope, mechanism, auth_data):
    # Ephemeral test credentials only; the inbox is not exposed publicly.
    return AuthResult(success=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=2525)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--outbox", required=True)
    args = parser.parse_args()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)
    controller = Controller(
        Inbox(Path(args.outbox)),
        hostname=args.host,
        port=args.port,
        tls_context=context,
        require_starttls=True,
        authenticator=authenticate,
    )
    controller.start()
    print(f"Nexora test SMTP inbox listening on {args.host}:{args.port}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        controller.stop()

if __name__ == "__main__":
    main()
