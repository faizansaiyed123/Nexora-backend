"""Minimal local STARTTLS SMTP sink used only by isolated browser CI."""
from __future__ import annotations
import argparse, asyncio, json, ssl
from pathlib import Path

class Inbox:
    def __init__(self, output, cert, key):
        self.output=Path(output); self.output.parent.mkdir(parents=True,exist_ok=True)
        self.ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); self.ctx.load_cert_chain(cert,key)
    async def reply(self, writer, line):
        writer.write((line+"\r\n").encode("ascii","replace")); await writer.drain()
    async def handle(self, reader, writer):
        secure=False; sender=""; recipients=[]
        await self.reply(writer,"220 localhost Nexora isolated mail sink ready")
        try:
            while line:=await reader.readline():
                command=line.decode("utf-8","replace").strip(); upper=command.upper()
                if upper.startswith(("EHLO ","HELO ")):
                    replies=("250-localhost","250-AUTH PLAIN LOGIN","250 SIZE 10485760") if secure else ("250-localhost","250-STARTTLS","250 SIZE 10485760")
                    for item in replies: await self.reply(writer,item)
                elif upper=="STARTTLS":
                    await self.reply(writer,"220 Ready to start TLS"); await writer.start_tls(self.ctx); secure=True
                elif upper.startswith("AUTH PLAIN"):
                    await self.reply(writer,"235 2.7.0 Authentication successful")
                elif upper=="AUTH LOGIN":
                    await self.reply(writer,"334 VXNlcm5hbWU6"); await reader.readline()
                    await self.reply(writer,"334 UGFzc3dvcmQ6"); await reader.readline()
                    await self.reply(writer,"235 2.7.0 Authentication successful")
                elif upper.startswith("MAIL FROM:"):
                    sender=command.split(":",1)[1].split(" ",1)[0].strip("<>"); recipients=[]; await self.reply(writer,"250 2.1.0 Sender accepted")
                elif upper.startswith("RCPT TO:"):
                    recipients.append(command.split(":",1)[1].split(" ",1)[0].strip("<>")); await self.reply(writer,"250 2.1.5 Recipient accepted")
                elif upper=="DATA":
                    await self.reply(writer,"354 End data with <CR><LF>.<CR><LF>"); parts=[]
                    while raw:=await reader.readline():
                        if raw in (b".\r\n",b".\n"): break
                        if raw.startswith(b".."): raw=raw[1:]
                        parts.append(raw.decode("utf-8","replace"))
                    row={"mail_from":sender,"rcpt_tos":recipients,"data":"".join(parts)}
                    with self.output.open("a",encoding="utf-8") as f: f.write(json.dumps(row,ensure_ascii=False)+"\n")
                    await self.reply(writer,"250 2.0.0 Message accepted for isolated review inbox")
                elif upper=="RSET":
                    sender=""; recipients=[]; await self.reply(writer,"250 2.0.0 Reset")
                elif upper=="NOOP": await self.reply(writer,"250 2.0.0 OK")
                elif upper=="QUIT":
                    await self.reply(writer,"221 2.0.0 Bye"); break
                else: await self.reply(writer,"250 2.0.0 OK")
        except (ConnectionError,asyncio.IncompleteReadError,ssl.SSLError): pass
        finally:
            writer.close()
            try: await writer.wait_closed()
            except (ConnectionError,ssl.SSLError): pass

async def main(args):
    server=await asyncio.start_server(Inbox(args.outbox,args.cert,args.key).handle,args.host,args.port)
    print(f"STARTTLS test inbox listening on {args.host}:{args.port}",flush=True)
    async with server: await server.serve_forever()

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--host",default="127.0.0.1"); parser.add_argument("--port",type=int,default=2525)
    parser.add_argument("--cert",required=True); parser.add_argument("--key",required=True); parser.add_argument("--outbox",required=True)
    asyncio.run(main(parser.parse_args()))
