"""Minimal in-process ASGI test client with a cookie jar (no httpx / requests dependency)."""
import asyncio
import json
from http.cookies import SimpleCookie
from urllib.parse import urlsplit


class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def json(self):
        return json.loads(self.body or b"null")

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")


class Client:
    def __init__(self, app, csrf=True):
        self.app, self.cookies, self.csrf = app, {}, csrf

    def request(self, method, url, json_body=None, headers=None, csrf=None):
        parts = urlsplit(url)
        hdrs = {"host": "testserver", **{k.lower(): v for k, v in (headers or {}).items()}}
        if self.cookies:
            hdrs["cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        body = b""
        if json_body is not None:
            body = json.dumps(json_body).encode()
            hdrs["content-type"] = "application/json"
        if (self.csrf if csrf is None else csrf) and method != "GET":
            hdrs["x-requested-with"] = "routing-app"
        hdrs["content-length"] = str(len(body))
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                 "path": parts.path, "raw_path": parts.path.encode(), "query_string": parts.query.encode(),
                 "root_path": "", "scheme": "http", "server": ("testserver", 80), "client": ("127.0.0.1", 5000),
                 "headers": [(k.encode(), v.encode()) for k, v in hdrs.items()], "state": {}}
        out = {"status": None, "headers": [], "body": b""}

        async def receive():
            nonlocal body
            if body is not None:
                chunk, body = body, None
                return {"type": "http.request", "body": chunk, "more_body": False}
            await asyncio.sleep(0)
            return {"type": "http.disconnect"}

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"], out["headers"] = msg["status"], msg.get("headers", [])
            elif msg["type"] == "http.response.body":
                out["body"] += msg.get("body", b"")

        asyncio.run(self.app(scope, receive, send))
        headers_out = {}
        for k, v in out["headers"]:
            k, v = k.decode().lower(), v.decode()
            if k == "set-cookie":
                c = SimpleCookie()
                c.load(v)
                for name, morsel in c.items():
                    if morsel.value in ("", "null") or "Max-Age=0" in v:
                        self.cookies.pop(name, None)
                    else:
                        self.cookies[name] = morsel.value
            headers_out[k] = v
        return Response(out["status"], headers_out, out["body"])

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, json_body=None, **kw):
        return self.request("POST", url, json_body if json_body is not None else {}, **kw)

    def put(self, url, json_body=None, **kw):
        return self.request("PUT", url, json_body if json_body is not None else {}, **kw)

    def delete(self, url, **kw):
        return self.request("DELETE", url, **kw)
