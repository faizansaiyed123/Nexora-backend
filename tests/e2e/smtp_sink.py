"""Ephemeral local STARTTLS inbox used only by the Nexora browser workflow."""
from __future__ import annotations
import argparse, json, ssl, time
from pathlib import Path
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult

class OutboxHandler:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
    async def handle_DATA(self, server, session, envelope):
        content = envelope.content
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        row = {"mail_from": envelope.mail_from, "rcpt_tos": envelope.rcpt_tos, "data": content}
        with self.path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
        return "250 Message accepted for the isolated Nexora review inbox"

def accept_test_credentials(server, session, envelope, mechanism, auth_data):
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
    controller = Controller(OutboxHandler(Path(args.outbox)), hostname=args.host, port=args.port,
        tls_context=context, require_starttls=True, authenticator=accept_test_credentials)
    controller.start()
    print(f"STARTTLS inbox listening on {args.host}:{args.port}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        controller.stop()

if __name__ == "__main__":
    main()
