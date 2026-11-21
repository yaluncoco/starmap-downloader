"""Async filesystem failures retain safe OS codes without disclosing error payloads."""
import errno
import json


def test_search_io_diagnostic_excludes_paths_and_exception_message(client, monkeypatch):
    private = "secret-key-in-network-payload"
    error = OSError(errno.EACCES, private, "C:/private/" + private)
    error.winerror = 32

    def fail(*args, **kwargs):
        raise error

    client.post("connect", {"client_id": "mock-client", "client_secret": "mock-secret"})
    area = client.post("aoi", {"coordinates": "116.350,39.900,116.355,39.905"})
    monkeypatch.setattr("core.service.find_scenes", fail)
    job = client.post("search", {"geometry": area["geometry"], "start": "2024-06-01",
                                 "end": "2024-06-10", "max_cloud": 100})
    failed = client.wait(job["job_id"], status="failed")
    assert "系统代码 32" in failed["error"]
    assert failed["io_diagnostic"]["errno"] == errno.EACCES
    assert failed["io_diagnostic"]["winerror"] == 32
    assert any(frame["module"] == "core.service" and frame["function"] == "_search_profile"
               for frame in failed["io_diagnostic"]["locations"])
    assert private not in json.dumps(failed)
    assert "filename" not in failed["io_diagnostic"]
