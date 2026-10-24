"""STARTTLS email receiver for the isolated Nexora browser workflow."""
from __future__ import annotations
import argparse
import json
import ssl
import time
from pathlib import Path
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult

class OutboxHandler:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def handle_DATA(self, server, session, envelope):
        content = envelope.content
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        with self.path.open("a", encoding="utf-8") as out:
            out.write(json.dumps({"mail_from": envelope.mail_from, "rcpt_tos": envelope.rcpt_tos, "data": content}, ensure_ascii=False) + "\n")
        return "250 Accepted by the isolated Nexora inbox"

def accept_ci_credentials(server, session, envelope, mechanism, auth_data):
    return AuthResult(success=True)

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2525)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--outbox", required=True)
    args = parser.parse_args()
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(args.cert, args.key)
    controller = Controller(
        OutboxHandler(Path(args.outbox)),
        hostname=args.host,
        port=args.port,
        tls_context=tls,
        require_starttls=True,
        auth_required=True,
        authenticator=accept_ci_credentials,
    )
    controller.start()
    print(f"STARTTLS inbox ready at {args.host}:{args.port}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        controller.stop()

if __name__ == "__main__":
    main()
