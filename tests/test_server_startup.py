"""The desktop's startup request burst must not reject its bootstrap connection."""
import http.client
import json
import threading
from concurrent.futures import ThreadPoolExecutor

from app import make_server


def test_local_startup_burst_waits_for_accept_without_refusing_connections(tmp_path):
    server = make_server(config_dir=tmp_path / "settings")
    ready, accepted = threading.Event(), threading.Event()
    lock = threading.Lock()
    connected = 0
    count = 24
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    started = False

    def request():
        nonlocal connected
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.connect()
            connection.request("GET", "/health")
            with lock:
                connected += 1
                if connected == count:
                    ready.set()
            assert accepted.wait(5), "The startup request burst did not reach the local server"
            response = connection.getresponse()
            assert response.status == 200 and json.loads(response.read()) == {"ok": True}
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=count) as pool:
        pending = [pool.submit(request) for _ in range(count)]
        try:
            # Simulate the brief accept delay while Edge starts its resource,
            # map and bootstrap connections. Every queued client must survive.
            assert ready.wait(3), f"Only {connected}/{count} startup connections were queued"
            worker.start()
            started = True
            accepted.set()
            for future in pending:
                future.result(timeout=5)
        finally:
            accepted.set()
            if started:
                server.shutdown()
            server.server_close()
            server.service.shutdown()
            if started:
                worker.join(3)
