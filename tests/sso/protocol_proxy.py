"""A real forwarding HTTP proxy for manipulating Keycloak token exchanges.

No validator/network mock: authorization, code exchange, userinfo and JWKS reach
Keycloak. The selected mutation is applied only to the actual HTTP exchange.
"""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode
from urllib.request import Request, urlopen


def _b64decode(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def mutate_token(token, mutation):
    header, payload, signature = token.split('.')
    if mutation == 'signature':
        signature = base64.urlsafe_b64encode(b'invalid-signature').decode().rstrip('=')
    else:
        claims = json.loads(_b64decode(payload))
        if mutation == 'issuer':
            claims['iss'] = 'http://127.0.0.1:8080/realms/skeleton-b'
        elif mutation == 'audience':
            claims['aud'] = 'wrong-client'
        elif mutation == 'expiry':
            claims['exp'] = 1
        else:
            raise ValueError(mutation)
        # Preserve the real signature: validation must reject modified claims.
        payload = base64.urlsafe_b64encode(json.dumps(claims,separators=(',',':')).encode()).decode().rstrip('=')
    return '.'.join((header,payload,signature))


class ExchangeProxy:
    def __init__(self, mutation=None, port=18080):
        self.mutation = mutation
        self.origin = f'http://127.0.0.1:{port}'
        self.token_requests = []
        self.token_responses = []
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                # Never log authorization codes, tokens, client credentials or assertions.
                pass

            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                body = self.rfile.read(int(self.headers.get('Content-Length',0))) if self.command == 'POST' else None
                token_request = self.path.endswith('/protocol/openid-connect/token')
                if token_request:
                    fields = parse_qs(body.decode())
                    proxy.token_requests.append(fields)
                    if proxy.mutation == 'pkce':
                        fields['code_verifier'] = ['wrong-verifier-'+'a'*43]
                        body = urlencode(fields,doseq=True).encode()
                headers = {name:value for name,value in self.headers.items() if name.lower() not in {'host','content-length','connection','accept-encoding'}}
                request = Request('http://127.0.0.1:8080'+self.path, data=body, headers=headers, method=self.command)
                try:
                    response = urlopen(request,timeout=15)
                except HTTPError as exc:
                    response = exc
                with response:
                    content = response.read()
                    status = response.status
                    content_type = response.headers.get('Content-Type','application/octet-stream')
                    if self.path.endswith('/.well-known/openid-configuration') and status == 200:
                        document = json.loads(content)
                        for key in ('token_endpoint','userinfo_endpoint','jwks_uri'):
                            document[key] = document[key].replace('http://127.0.0.1:8080',proxy.origin)
                        content = json.dumps(document).encode()
                    elif token_request:
                        proxy.token_responses.append(status)
                        if status == 200 and proxy.mutation in {'signature','issuer','audience','expiry'}:
                            document = json.loads(content)
                            document['id_token'] = mutate_token(document['id_token'],proxy.mutation)
                            content = json.dumps(document).encode()
                    self.send_response(status)
                    self.send_header('Content-Type',content_type)
                    self.send_header('Content-Length',str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
        self.server = ThreadingHTTPServer(('127.0.0.1',port),Handler)
        self.thread = Thread(target=self.server.serve_forever,daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self,*args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
