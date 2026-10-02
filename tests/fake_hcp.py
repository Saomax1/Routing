"""
A stand-in Housecall Pro API server for tests: real HTTP on localhost, so the live client (auth header, paging,
status and date filters) runs exactly as it would against the real service.

It answers GET /employees and GET /jobs, and RECORDS every request it receives. Anything that is not a GET is
answered 405 and recorded too, which is how the tests prove the app only ever reads.

Its jobs and employees are the synthetic demo data with every id rewritten so they look like real Housecall Pro ids
(``job_<hex>`` / ``pro_<hex>``, never ``*_demo_*``): the app must treat them as real customers.
"""
import hashlib
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from app.domain.timeutil import parse_iso
from app.hcp.fixtures import make_demo_dataset


def _hex(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def real_looking_dataset(now: datetime) -> dict:
    ds = make_demo_dataset(now=now)
    emp = {e["id"]: f"pro_{_hex(e['id'])}" for e in ds["employees"]}
    for e in ds["employees"]:
        e["id"] = emp[e["id"]]
    for j in ds["jobs"]:
        j["id"] = f"job_{_hex(j['id'])}"
        for a in j.get("assigned_employees") or []:
            a["id"] = emp.get(a["id"], a["id"])
    return ds


class FakeHcp:
    def __init__(self, key: str = "test-key-123", now: datetime = None, dataset: dict = None, port: int = 0):
        self.key = key
        self.dataset = dataset or real_looking_dataset(now or datetime.now(timezone.utc))
        self.requests, self._lock = [], threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):          # keep the test output quiet
                pass

            def _answer(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self):
                url = urlparse(self.path)
                query = parse_qs(url.query, keep_blank_values=True)
                with outer._lock:
                    outer.requests.append({"method": self.command, "path": url.path, "query": query,
                                           "authorization": self.headers.get("Authorization")})
                if self.command != "GET":
                    return self._answer(405, {"error": "this fake server only allows GET"})
                if self.headers.get("Authorization") != f"Token {outer.key}":
                    return self._answer(401, {"error": "Unauthorized"})
                if url.path == "/employees":
                    return self._answer(200, outer._page("employees", outer.dataset["employees"], query))
                if url.path == "/jobs":
                    return self._answer(200, outer._page("jobs", outer._jobs(query), query))
                return self._answer(404, {"error": "not found"})

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    def _jobs(self, query):
        statuses = set(query.get("work_status[]", []))
        lo = parse_iso((query.get("scheduled_start_min") or [None])[0])
        hi = parse_iso((query.get("scheduled_start_max") or [None])[0])
        out = []
        for j in self.dataset["jobs"]:
            if statuses and j["work_status"] not in statuses:
                continue
            if lo or hi:
                start = parse_iso((j.get("schedule") or {}).get("scheduled_start"))
                if not start or (lo and start < lo) or (hi and start >= hi):
                    continue
            out.append(j)
        return out

    @staticmethod
    def _page(kind, items, query):
        size = int((query.get("page_size") or [100])[0])
        page = int((query.get("page") or [1])[0])
        total_pages = max(1, -(-len(items) // size))
        return {"page": page, "page_size": size, "total_pages": total_pages, "total_items": len(items),
                kind: items[(page - 1) * size: page * size]}

    # -- test helpers
    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def methods(self):
        return {r["method"] for r in self.requests}

    def customer_text(self) -> list:
        """Strings that would identify a customer (names, street, phone), to prove reports do not carry them."""
        out = []
        for j in self.dataset["jobs"]:
            c, a = j.get("customer") or {}, j.get("address") or {}
            out += [c.get("first_name"), c.get("last_name"), c.get("mobile_number"), a.get("street")]
        return sorted({x for x in out if x and len(str(x)) > 3})
