"""Standard-library SMTP sink with STARTTLS for isolated Nexora E2E."""
from __future__ import annotations

import argparse
import asyncio
import json
import ssl
from pathlib import Path


class SMTPInbox:
    def __init__(self, outbox: Path, cert: Path, key: Path) -> None:
        self.outbox = outbox
        self.outbox.parent.mkdir(parents=True, exist_ok=True)
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.load_cert_chain(certfile=str(cert), keyfile=str(key))

    async def connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        mail_from = ""
        rcpt_tos: list[str] = []
        await self.reply(writer, "220 localhost Nexora isolated mail inbox")
        while not writer.is_closing():
            try:
                raw = await reader.readline()
            except (ConnectionError, ssl.SSLError):
                break
            if not raw:
                break
            command = raw.decode("utf-8", "replace").strip()
            upper = command.upper()
            if upper.startswith(("EHLO ", "HELO ")):
                writer.write(b"250-localhost\r\n250-STARTTLS\r\n250-AUTH PLAIN LOGIN\r\n250 SIZE 10485760\r\n")
                await writer.drain()
            elif upper == "STARTTLS":
                await self.reply(writer, "220 Ready to start TLS")
                try:
                    await writer.start_tls(self.tls)
                except (ConnectionError, ssl.SSLError, RuntimeError):
                    break
            elif upper.startswith("AUTH PLAIN"):
                token = command.split(" ", 2)[2] if len(command.split(" ", 2)) == 3 else ""
                if not token:
                    await self.reply(writer, "334 ")
                    token = (await reader.readline()).decode("ascii", "replace").strip()
                await self.reply(writer, "235 2.7.0 Authentication successful")
            elif upper == "AUTH LOGIN":
                await self.reply(writer, "334 VXNlcm5hbWU6")
                await reader.readline()
                await self.reply(writer, "334 UGFzc3dvcmQ6")
                await reader.readline()
                await self.reply(writer, "235 2.7.0 Authentication successful")
            elif upper.startswith("MAIL FROM:"):
                mail_from = command.split(":", 1)[1].strip().strip("<>")
                await self.reply(writer, "250 Sender accepted")
            elif upper.startswith("RCPT TO:"):
                rcpt_tos.append(command.split(":", 1)[1].strip().strip("<>"))
                await self.reply(writer, "250 Recipient accepted")
            elif upper == "DATA":
                await self.reply(writer, "354 End data with <CR><LF>.<CR><LF>")
                chunks: list[str] = []
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    decoded = line.decode("utf-8", "replace").rstrip("\r\n")
                    if decoded == ".":
                        break
                    if decoded.startswith(".."):
                        decoded = decoded[1:]
                    chunks.append(decoded)
                row = {"mail_from": mail_from, "rcpt_tos": rcpt_tos, "data": "\n".join(chunks)}
                with self.outbox.open("a", encoding="utf-8") as output:
                    output.write(json.dumps(row, ensure_ascii=False) + "\n")
                mail_from, rcpt_tos = "", []
                await self.reply(writer, "250 Message accepted")
            elif upper == "RSET":
                mail_from, rcpt_tos = "", []
                await self.reply(writer, "250 Reset")
            elif upper == "NOOP":
                await self.reply(writer, "250 OK")
            elif upper == "QUIT":
                await self.reply(writer, "221 Bye")
                break
            else:
                await self.reply(writer, "250 OK")
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, ssl.SSLError):
            pass

    @staticmethod
    async def reply(writer: asyncio.StreamWriter, message: str) -> None:
        writer.write(message.encode("utf-8") + b"\r\n")
        await writer.drain()

    async def run(self, host: str, port: int) -> None:
        server = await asyncio.start_server(self.connection, host, port)
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
    asyncio.run(SMTPInbox(Path(args.outbox), Path(args.cert), Path(args.key)).run(args.host, args.port))


if __name__ == "__main__":
    main()
