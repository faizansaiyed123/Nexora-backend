"""An isolated SMTP STARTTLS inbox for Nexora's disposable browser environment."""
import argparse,json,ssl,time
from pathlib import Path
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult

class Handler:
    def __init__(self,path:Path):
        self.path=path
        self.path.parent.mkdir(parents=True,exist_ok=True)
    async def handle_DATA(self,server,session,envelope):
        data=envelope.content
        if isinstance(data,bytes): data=data.decode("utf-8","replace")
        record={"mail_from":envelope.mail_from,"rcpt_tos":envelope.rcpt_tos,"data":data}
        with self.path.open("a",encoding="utf-8") as out:
            out.write(json.dumps(record,ensure_ascii=False)+"\n")
        return "250 Accepted by the isolated Nexora inbox"

def authenticate(server,session,envelope,mechanism,auth_data):
    return AuthResult(success=True)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--host",default="127.0.0.1")
    parser.add_argument("--port",type=int,default=2525)
    parser.add_argument("--cert",required=True)
    parser.add_argument("--key",required=True)
    parser.add_argument("--outbox",required=True)
    args=parser.parse_args()
    tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(args.cert,args.key)
    controller=Controller(Handler(Path(args.outbox)),hostname=args.host,port=args.port,
        tls_context=tls,require_starttls=True,authenticator=authenticate)
    controller.start()
    print(f"STARTTLS inbox ready at {args.host}:{args.port}",flush=True)
    try:
        while True: time.sleep(3600)
    except KeyboardInterrupt: controller.stop()

if __name__=="__main__": main()
