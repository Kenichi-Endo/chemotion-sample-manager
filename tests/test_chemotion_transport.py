"""Exercise the real chemotionapi connection without contacting a server."""

import json
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import chemotion_api
import pytest
from requests import Response
from requests.exceptions import Timeout

from chemotion_sample_manager import client as api_client


@pytest.fixture(autouse=True)
def isolated_authentication(monkeypatch):
    monkeypatch.setattr(api_client, "_authentication_key", None)
    monkeypatch.setattr(api_client, "_authentication_token", None)


def test_clients_reuse_token_until_connection_details_change(monkeypatch):
    calls = []

    def login(instance, username, password):
        calls.append((username, password))
        instance._con.set_bearer_token(f"token-{len(calls)}")

    monkeypatch.setattr(api_client.Instance, "login", login)
    first = api_client.ChemotionClient()
    first.login()
    second = api_client.ChemotionClient()
    second.login()
    assert first.connection is not second.connection
    assert second.connection.bearer_token == first.connection.bearer_token
    assert len(calls) == 1
    monkeypatch.setattr(api_client, "CHEMOTION_USERNAME", "another-user")
    second.login()
    assert len(calls) == 2
    assert second.connection.bearer_token == "token-2"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api_client, "CHEMOTION_URL", "https://eln.example.org")
    return api_client.ChemotionClient()


def test_login_and_inventory_requests_use_chemotionapi_token(client, monkeypatch):
    monkeypatch.setattr(api_client, "CHEMOTION_USERNAME", "example")
    monkeypatch.setattr(api_client, "CHEMOTION_PASSWORD", "test-password")
    requests = []

    def send(request, **kwargs):
        requests.append(request)
        assert kwargs["timeout"] == api_client.REQUEST_TIMEOUT_SECONDS
        assert kwargs["verify"] is True
        response = Response()
        response.request = request
        response.status_code = 201 if request.url.endswith("/token") else 200
        response._content = b'{"token":"test-token"}' if response.status_code == 201 else b'{}'
        if request.url.endswith("/about"):
            response._content = b"Version: 4.0.1"
        return response

    monkeypatch.setattr(client.connection, "send", send)
    client.login()
    client.request("/api/v1/samples")
    client.request("/api/v1/samples/1", b'{"name":"updated"}',
                   {"Content-Type": "application/json"}, "PUT")
    client.request("https://eln.example.org/images/molecules/1.svg")
    assert requests[1].url == "https://eln.example.org/api/v1/public/token"
    assert parse_qs(requests[1].body) == {"username": ["example"], "password": ["test-password"]}
    for request in requests[2:]:
        assert request.headers["Authorization"] == "Bearer test-token"
    assert json.loads(requests[3].body) == {"name": "updated"}
    assert requests[4].url == "https://eln.example.org/images/molecules/1.svg"


@pytest.mark.parametrize("url", ["https://other.example.org/image.svg", "//other.example.org/image.svg",
                                 "http://eln.example.org/image.svg"])
def test_external_urls_are_rejected_before_sending_credentials(client, monkeypatch, url):
    def send(*args, **kwargs):
        pytest.fail("An off-site request must not be sent")

    monkeypatch.setattr(client.connection, "send", send)
    with pytest.raises(ConnectionError, match="outside"):
        client.request(url)


@pytest.mark.parametrize("method, attempts", [("GET", 3), ("PUT", 1), ("POST", 1), ("DELETE", 1)])
def test_transport_failure_only_retries_reads(client, monkeypatch, method, attempts):
    calls = []

    def send(request, **kwargs):
        calls.append(request)
        raise Timeout("Timed out")

    monkeypatch.setattr(client.connection, "send", send)
    monkeypatch.setattr(api_client.time, "sleep", lambda seconds: None)
    with pytest.raises(ConnectionError):
        client.request("/api/v1/samples/1", method=method)
    assert len(calls) == attempts


@pytest.fixture
def sample_server(client, monkeypatch):
    """Run real chemotionapi objects against an in-memory server response."""
    template = Path(chemotion_api.__file__).parent / "elements" / "empty_elements" / "sample.json"
    sample = json.loads(template.read_text())
    sample.update({
        "id": 7, "is_new": False, "short_label": "S7", "name": "Original",
        "container": {"id": 3, "container_type": "root", "children": [
            {"id": 4, "container_type": "analyses", "children": []}]},
        "decoupled": True, "inventory_sample": False,
        "real_amount_value": "12", "real_amount_unit": "mg",
        "target_amount_value": "20", "target_amount_unit": "mg",
        "description": "Keep this description", "molecule_name_id": 11, "molfile": "sample molfile",
        "molecule": {"id": 9, "molfile": "original molfile", "cano_smiles": "CCO"},
    })
    calls = []

    def send(request, **kwargs):
        path = urlsplit(request.url).path
        calls.append((request.method, path, request.body))
        response = Response()
        response.request = request
        response.status_code = 200
        if path == "/about":
            response._content = b"Version: 4.0.1"
            return response
        if path == "/api/v1/generic_elements/klasses.json":
            payload = {"klass": [{"name": "sample", "is_generic": False}]}
        elif path == "/api/v1/segments/klasses.json":
            payload = {"klass": []}
        elif path in {"/api/v1/samples/7.json", "/api/v1/samples/7"}:
            payload = {"sample": sample}
        elif path == "/api/v1/chemicals":
            payload = {"id": 5, "chemical_data": [{"vendor": "Vendor"}]}
        else:
            pytest.fail(f"Unexpected request: {request.method} {path}")
        response._content = json.dumps(payload).encode()
        return response

    monkeypatch.setattr(client.connection, "send", send)
    return sample, calls


def test_inventory_detail_uses_library_sample_loading(client, sample_server):
    sample, calls = sample_server
    row = client.inventory_entry(7, 42)
    assert row["id"] == 7
    assert row["sample"] == "Original"
    assert row["vendor"] == "Vendor"
    assert any(path == "/api/v1/samples/7.json" for _, path, _ in calls)


def test_prefetched_details_skip_individual_sample_request(client, sample_server):
    sample, calls = sample_server
    row = client.inventory_entry(7, 42, detail=sample)
    assert row["sample"] == "Original"
    assert row["vendor"] == "Vendor"
    assert [path for _, path, _ in calls] == ["/api/v1/chemicals"]


def test_structure_copy_and_paste_updates_molecule_and_sample_svg(client, sample_server, monkeypatch):
    sample, _ = sample_server
    source = {"id": 8, "molfile": "source drawing", "molecule": {"id": 19, "molfile": "molecule default"},
              "molecule_name_hash": {"value": 21, "label": "Source molecule"}}
    original_send = client.connection.send
    svg_requests = []
    saved = []
    svg_file = "a" * 128 + ".svg"

    def send(request, **kwargs):
        path = urlsplit(request.url).path
        if path == "/api/v1/samples/8.json":
            payload = {"sample": {**sample, **source}}
        elif path == "/api/v1/samples/batch-refresh-svg":
            svg, = json.loads(request.body)["svgs"]
            svg_requests.append(svg)
            payload = {"results": [{"success": True, "filename": svg["svg_path"]}]}
        elif path == "/api/v1/samples/7" and request.method == "PUT":
            body = json.loads(request.body)
            saved.append(body)
            sample.update(molfile=body["molfile"], molecule={"id": body["molecule_id"]},
                          sample_svg_file=svg_file)
            payload = {"sample": sample}
        else:
            return original_send(request, **kwargs)
        response = Response()
        response.request = request
        response.status_code = 200
        response._content = json.dumps(payload).encode()
        return response

    monkeypatch.setattr(client.connection, "send", send)
    structure, name = client.get_sample_structure(8)
    assert name == "Source molecule"
    refreshed = client.set_sample_structure(7, structure)

    svg, = svg_requests
    assert svg["molfile"] == "source drawing"
    body, = saved
    assert body["sample_svg_file"] == svg["svg_path"]
    assert body["molecule_id"] == 19
    assert body["molecule_name_id"] == 21
    assert body["molfile"] == "source drawing"
    assert body["name"] == "Original"
    assert body["container"] == sample["container"]
    assert body["decoupled"] is True
    assert refreshed["sample_svg_file"] == svg_file


@pytest.mark.parametrize("method, args, expected", [
    ("set_location", ("New",), {"location": "New"}),
    ("set_density", ("1.25",), {"density": 1.25}),
    ("set_sample_name", ("Renamed",), {"name": "Renamed"}),
    ("set_sample_name", ("",), {"name": ""}),
    ("set_molecule_name", (12,), {"molecule_name_id": 12}),
    ("set_amount", ("real_amount", "0", "g"), {"real_amount_value": 0.0, "real_amount_unit": "g"}),
    ("set_amount", ("target_amount", "250", "mg"), {"target_amount_value": 250.0, "target_amount_unit": "mg"}),
])
def test_library_save_preserves_current_sample_data(client, sample_server, method, args, expected):
    sample, calls = sample_server
    # Deliberately stale GUI flags/container must not replace server values.
    getattr(client, method)(7, *args, {"id": 999}, False, True)
    writes = [(verb, path, json.loads(body)) for verb, path, body in calls if verb != "GET"]
    assert len(writes) == 1
    verb, path, body = writes[0]
    assert verb == "PUT" and path == "/api/v1/samples/7"
    for key, value in expected.items():
        assert body[key] == value
    assert body["container"] == sample["container"]
    assert body["decoupled"] is True
    assert body["inventory_sample"] is False
    assert body["description"] == "Keep this description"
    assert body["molfile"] == "sample molfile"
    for key in ("real_amount_value", "real_amount_unit", "target_amount_value", "target_amount_unit", "molecule_name_id"):
        if key not in expected:
            assert body[key] == (float(sample[key]) if key.endswith("_value") else sample[key])


def test_library_save_updates_flags_together(client, sample_server):
    sample, calls = sample_server
    client.set_sample_flags(7, {"id": 999}, False, True)
    body = json.loads([body for method, _, body in calls if method == "PUT"][0])
    assert body["decoupled"] is False
    assert body["inventory_sample"] is True
    assert body["container"] == sample["container"]


def test_library_save_preserves_an_empty_analysis_container(client, sample_server):
    sample, calls = sample_server
    sample["container"]["children"] = []
    client.set_sample_name(7, "Renamed", None, False, False)
    body = json.loads(next(body for method, _, body in calls if method == "PUT"))
    assert body["name"] == "Renamed"
    assert body["container"] == sample["container"]


def test_library_save_retains_null_sample_structure(client, sample_server):
    sample, calls = sample_server
    sample["molfile"] = None
    client.set_location(7, "New", {}, False, True)
    body = json.loads([body for method, _, body in calls if method == "PUT"][0])
    assert body["molfile"] is None


@pytest.mark.parametrize("method, args", [
    ("set_location", ("New", {}, False, True)),
    ("set_amount", ("real_amount", "5", "mg", {}, False, True)),
    ("set_sample_flags", ({}, False, True)),
])
@pytest.mark.parametrize("status, permission, lookup_status, expected_reads, error_type, message", [
    (401, False, 200, 2, PermissionError, "does not have write permission"),
    (403, False, 200, 2, PermissionError, "does not have write permission"),
    (401, True, 200, 2, ConnectionError, "Chemotion returned 401"),
    (401, None, 200, 2, ConnectionError, "Chemotion returned 401"),
    (401, None, 401, 2, ConnectionError, "Chemotion returned 401"),
    (422, False, 200, 1, ConnectionError, "Chemotion returned 422"),
])
def test_sample_write_checks_permission_after_rejection(
        client, sample_server, monkeypatch, method, args, status, permission, lookup_status,
        expected_reads, error_type, message):
    sample, _calls = sample_server
    send_sample_response = client.connection.send
    sample_requests = []

    def send(request, **kwargs):
        path = urlsplit(request.url).path
        if path in {"/api/v1/samples/7", "/api/v1/samples/7.json"}:
            sample_requests.append(request.method)
            if request.method == "PUT" or (len(sample_requests) == 3 and lookup_status != 200):
                response = Response()
                response.request = request
                response.status_code = status if request.method == "PUT" else lookup_status
                response._content = b'{"error":"Request rejected"}'
                return response
            if len(sample_requests) == 3 and permission is not None:
                sample["can_update"] = permission
        return send_sample_response(request, **kwargs)

    monkeypatch.setattr(client.connection, "send", send)

    with pytest.raises(error_type, match=message):
        getattr(client, method)(7, *args)

    assert sample_requests == ["GET", "PUT"] + ["GET"] * (expected_reads - 1)


@pytest.mark.parametrize("chemical_id, method, path", [
    (None, "POST", "/api/v1/chemicals/create"),
    (5, "PUT", "/api/v1/chemicals"),
])
def test_vendor_write_checks_sample_permission_after_rejection(
        client, sample_server, monkeypatch, chemical_id, method, path):
    sample, _calls = sample_server
    sample["can_update"] = False
    send_sample_response = client.connection.send
    requests = []

    def send(request, **kwargs):
        requests.append((request.method, urlsplit(request.url).path))
        if request.method == "GET":
            return send_sample_response(request, **kwargs)
        response = Response()
        response.request = request
        response.status_code = 403
        response._content = b'{"error":"Request rejected"}'
        return response

    monkeypatch.setattr(client.connection, "send", send)
    with pytest.raises(PermissionError, match="does not have write permission"):
        client.set_vendor(7, [], "Vendor", chemical_id=chemical_id)
    assert requests == [(method, path), ("GET", "/api/v1/samples/7.json")]


@pytest.mark.parametrize("shared", [False, True])
def test_collection_picker_uses_library_trees_and_retains_all(client, monkeypatch, shared):
    paths = []
    payloads = {
        "/api/v1/collections/": {
            "own": [{"id": 1, "label": "All", "is_locked": True},
                    {"id": 2, "label": "Group", "ancestry": "/"},
                    {"id": 3, "label": "Inventory", "ancestry": "/2/"}],
            "shared_with_me": [
                {"id": 4, "label": "Shared group", "ancestry": "/"},
                {"id": 5, "label": "Shared inventory", "ancestry": "/4/",
                 "owner_name": "Shared owner", "collection_share_id": 99},
                {"id": 3, "label": "Shared copy", "ancestry": "/", "owner_name": "Shared owner"},
            ] if shared else [],
        },
        "/api/v1/collections/all": {"collection": {"id": 1, "label": "All", "is_locked": True}},
    }

    def send(request, **kwargs):
        path = urlsplit(request.url).path
        paths.append(path)
        response = Response()
        response.status_code = 200
        response._content = json.dumps(payloads[path]).encode()
        return response

    monkeypatch.setattr(client.connection, "send", send)
    collections = client.collections()
    assert [(item["id"], item["_synchronized"]) for item in collections] == (
        [(1, False), (3, False), (3, True), (5, True)] if shared else [(1, False), (3, False)])
    if shared:
        assert collections[-1]["owner_name"] == "Shared owner"
    assert paths == list(payloads)


@pytest.mark.parametrize("synchronized", [False, True])
@pytest.mark.parametrize("headers", [False, True])
def test_library_pagination_loads_every_page(client, sample_server, monkeypatch, synchronized, headers):
    sample, calls = sample_server
    original_send = client.connection.send
    pages = []

    def send(request, **kwargs):
        if urlsplit(request.url).path != "/api/v1/samples.json":
            return original_send(request, **kwargs)
        query = parse_qs(request.body)
        assert query["collection_id"] == ["42"]
        page = int(query["page"][0])
        pages.append(page)
        numbers = range(1, 101) if page == 1 else (101,)
        response = Response()
        response.status_code = 200
        response._content = json.dumps({
            "samples": [dict(sample, id=number, short_label=f"S{number}") for number in numbers],
            "samples_count": 101,
        }).encode()
        if headers:
            response.headers.update({"X-Total": "101", "X-Total-Pages": "2"})
        return response

    monkeypatch.setattr(api_client.InventoryConnection, "send", lambda self, request, **kwargs: send(request, **kwargs))
    monkeypatch.setattr(client.connection, "send", send)
    monkeypatch.setattr(client, "login", lambda: None)
    detail_contexts = []
    def sample_details(sample_ids, collection_id):
        detail_contexts.append(collection_id)
        return {}
    monkeypatch.setattr(client, "sample_details", sample_details)
    monkeypatch.setattr(api_client.ChemotionClient, "inventory_entry", lambda self, sample_id, *args: {"id": sample_id})
    monkeypatch.setattr(api_client.ChemotionClient, "login", lambda self: None)
    progress = []
    rows = client.inventory(42, synchronized=synchronized, progress=progress.append)
    assert pages == [1, 2]
    assert [row["id"] for row in rows] == list(range(101, 0, -1))
    assert detail_contexts == [42]
    assert "Retrieving collection: 1 of 1…" in progress
    assert "Retrieving sample information: 101 of 101…" in progress



@pytest.mark.parametrize("synchronized", [False, True])
@pytest.mark.parametrize("status, locked", [(204, []), (200, [7]), (404, None), (403, None), (500, None)])
def test_library_removal_handles_locked_samples_and_never_retries(
        client, sample_server, monkeypatch, synchronized, status, locked):
    original_send = client.connection.send
    deletes = []

    def send(request, **kwargs):
        path = urlsplit(request.url).path
        response = Response()
        response.request = request
        response.status_code = 200
        if path == "/api/v1/collections/":
            key = "shared_with_me" if synchronized else "own"
            payload = {"own": [], "shared_with_me": []}
            payload[key] = [{"id": 42, "label": "Inventory"}]
        elif path == "/api/v1/collections/all":
            payload = {"collection": {"id": 1}}
        elif request.method == "DELETE":
            deletes.append((path, json.loads(request.body)))
            response.status_code = status if len(deletes) == 1 else 204
            payload = {"locked_sample_ids": locked} if locked is not None else {"error": "Request rejected"}
        else:
            return original_send(request, **kwargs)
        response._content = b"" if response.status_code == 204 else json.dumps(payload).encode()
        return response

    monkeypatch.setattr(client.connection, "send", send)
    if locked is None:
        with pytest.raises(ConnectionError):
            client.remove_sample_from_collection(42, 7, synchronized=synchronized)
    else:
        assert client.remove_sample_from_collection(42, 7, synchronized=synchronized) == {"locked_sample_ids": locked}
    assert len(deletes) == 1
    path, payload = deletes[0]
    assert path == "/api/v1/collection_elements/42"
    assert payload["ui_state"]["currentCollection"] == {"id": 42}
    assert payload["ui_state"]["sample"]["checkedIds"] == [7]
