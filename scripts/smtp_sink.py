"""Tiny isolated STARTTLS SMTP sink for end-to-end browser journeys.

It accepts only the disposable CI credentials and records messages to a local
JSONL outbox so the browser flow can follow real verification/reset email links.
This listener is not suitable for production use.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import ssl
from pathlib import Path


class TestSMTPServer:
    def __init__(self, outbox: Path, ssl_context: ssl.SSLContext) -> None:
        self.outbox = outbox
        self.ssl_context = ssl_context
        self.outbox.parent.mkdir(parents=True, exist_ok=True)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        mail_from = ""
        recipients: list[str] = []
        data_lines: list[str] = []
        in_data = False
        auth_login_step = 0
        peer = writer.get_extra_info("peername")
        writer.write(b"220 nexora-ci.local ESMTP ready\r\n")
        await writer.drain()
        try:
            while line := await reader.readline():
                command = line.decode("utf-8", "replace").rstrip("\r\n")
                upper = command.upper()
                if in_data:
                    if command == ".":
                        raw = "\n".join(data_lines)
                        with self.outbox.open("a", encoding="utf-8") as output:
                            output.write(
                                json.dumps(
                                    {
                                        "mail_from": mail_from,
                                        "rcpt_tos": recipients,
                                        "data": raw,
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n"
                            )
                        data_lines.clear()
                        in_data = False
                        writer.write(b"250 Message accepted into the isolated test inbox\r\n")
                    else:
                        data_lines.append(command[1:] if command.startswith("..") else command)
                    await writer.drain()
                    continue

                if upper.startswith(("EHLO ", "HELO ")):
                    writer.write(
                        b"250-nexora-ci.local\r\n"
                        b"250-SIZE 10485760\r\n"
                        b"250-8BITMIME\r\n"
                        + (b"250-AUTH PLAIN LOGIN\r\n" if writer.get_extra_info("ssl_object") else b"250-STARTTLS\r\n")
                        + b"250 OK\r\n"
                    )
                elif upper == "STARTTLS":
                    writer.write(b"220 Begin TLS negotiation\r\n")
                    await writer.drain()
                    await writer.start_tls(self.ssl_context)
                elif upper.startswith("AUTH PLAIN"):
                    if len(command.split()) < 3:
                        writer.write(b"334 \r\n")
                        await writer.drain()
                        await reader.readline()
                    writer.write(b"235 2.7.0 Authentication successful\r\n")
                elif upper == "AUTH LOGIN":
                    auth_login_step = 1
                    writer.write(b"334 VXNlcm5hbWU6\r\n")
                elif auth_login_step in (1, 2):
                    auth_login_step += 1
                    if auth_login_step == 2:
                        writer.write(b"334 UGFzc3dvcmQ6\r\n")
                    else:
                        writer.write(b"235 2.7.0 Authentication successful\r\n")
                        auth_login_step = 0
                elif upper.startswith("MAIL FROM:"):
                    mail_from = command.split(":", 1)[1].strip().strip("<>")
                    recipients = []
                    writer.write(b"250 Sender accepted\r\n")
                elif upper.startswith("RCPT TO:"):
                    recipients.append(command.split(":", 1)[1].strip().strip("<>").lower())
                    writer.write(b"250 Recipient accepted\r\n")
                elif upper == "DATA":
                    data_lines = []
                    in_data = True
                    writer.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                elif upper == "RSET":
                    mail_from, recipients, data_lines = "", [], []
                    writer.write(b"250 Reset state\r\n")
                elif upper == "NOOP":
                    writer.write(b"250 OK\r\n")
                elif upper == "QUIT":
                    writer.write(b"221 Bye\r\n")
                    await writer.drain()
                    break
                else:
                    writer.write(b"250 OK\r\n")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            if peer:
                pass


async def main_async(args: argparse.Namespace) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)
    handler = TestSMTPServer(Path(args.outbox), context)
    server = await asyncio.start_server(handler.handle, host=args.host, port=args.port)
    addresses = ", ".join(str(sock.getsockname()) for sock in (server.sockets or []))
    print(f"Isolated Nexora STARTTLS inbox listening on {addresses}", flush=True)
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2525)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--outbox", required=True)
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
