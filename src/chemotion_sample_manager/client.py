"""Chemotion API access and inventory retrieval."""
import json
import re
import secrets
import tempfile
import threading
import time
import io
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import contextmanager
from pathlib import Path
from queue import Empty, Queue
from urllib.parse import urlencode
from urllib.parse import urlsplit
from chemotion_api import Instance
from chemotion_api.connection import Connection, request_error
from chemotion_api.elements import ElementSet
from chemotion_api.elements.sample import Sample
from chemotion_api.elements.utils import GasType
from chemotion_api.generic_segments import GenericSegments
from requests.exceptions import RequestException
from .samples import (
    as_dict,
    chemical_cas,
    inventory_row,
    inventory_vendor,
    natural_sort_value,
    shown,
)


DEFAULT_CHEMOTION_URL = "https://"
CHEMOTION_URL = DEFAULT_CHEMOTION_URL
CHEMOTION_USERNAME = ""
CHEMOTION_PASSWORD = ""
_authentication_lock = threading.RLock()
_authentication_key = None
_authentication_token = None

# Keep concurrent inventory reads small enough to avoid TLS connection bursts.
MAX_CONCURRENT_SAMPLE_REQUESTS = 8
MAX_CONCURRENT_LIST_REQUESTS = 4
SAMPLE_DETAIL_BATCH_SIZE = 100
REQUEST_TIMEOUT_SECONDS = 45
TRANSIENT_REQUEST_ATTEMPTS = 3
# Retry transient server failures only for safe read requests.
TRANSIENT_SERVER_ERROR_CODES = frozenset((500, 502, 503, 504))


class InventoryLoadCancelled(Exception):
    """Raised internally when the user stops an in-progress inventory load."""
    def __init__(self, rows=None):
        self.rows = rows or []
        super().__init__()


def normalized_chemotion_url(value):
    """Return a safe Chemotion server root, suitable for API paths."""
    server = str(value or "").strip()
    if not server:
        raise ValueError("Enter the Chemotion server address.")
    server = server.rstrip("/")
    parts = urlsplit(server)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.path or parts.query or parts.fragment:
        raise ValueError("Enter a server address such as https://eln.example.org.")
    return server


class InventoryConnection(Connection):
    """chemotionapi transport with bounded reads and no automatic write retries."""

    def _send_request(self, method, url_path, default_header, kwargs):
        global _authentication_token
        # chemotionapi normally treats all paths as relative. Downloads and
        # thumbnails can supply absolute URLs; never send credentials off-site.
        target = urlsplit(url_path)
        if target.scheme or target.netloc:
            origin = urlsplit(self.host_url)
            if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
                raise ConnectionError("Chemotion returned a URL outside the configured server.")
            kwargs["url"] = url_path
        kwargs.setdefault("timeout", REQUEST_TIMEOUT_SECONDS)
        kwargs["headers"] = {**self.get_default_session_header(),
                             **kwargs.get("headers", default_header)}
        last_error = None
        for attempt in range(TRANSIENT_REQUEST_ATTEMPTS):
            try:
                response = super()._send_request(method, url_path, default_header, dict(kwargs))
                if response.status_code < 400:
                    # Older servers provide the total in JSON instead of the
                    # pagination headers consumed by chemotionapi's ElementSet.
                    if (method.lower() == "get" and target.path == "/api/v1/samples.json"
                            and ("X-Total" not in response.headers or "X-Total-Pages" not in response.headers)):
                        total = response.json().get("samples_count")
                        if total is not None:
                            per_page = int((kwargs.get("data") or {}).get("per_page", 100))
                            response.headers.setdefault("X-Total", str(total))
                            response.headers.setdefault("X-Total-Pages", str(max(1, (int(total) + per_page - 1) // per_page)))
                    return response
                error = request_error(response)
                response.close()
                if response.status_code == 401 and self.bearer_token is not None:
                    with _authentication_lock:
                        if self.bearer_token == _authentication_token:
                            _authentication_token = None
                if (response.status_code in TRANSIENT_SERVER_ERROR_CODES
                        and method.lower() in {"get", "head"}
                        and attempt + 1 < TRANSIENT_REQUEST_ATTEMPTS):
                    last_error = error
                    time.sleep(attempt + 1)
                    continue
                retries = (f" after {TRANSIENT_REQUEST_ATTEMPTS} attempts"
                           if last_error is not None else "")
                raise ConnectionError(f"Chemotion returned {response.status_code}{retries}: {error.snippet[:250]}") from error
            except (RequestException, ConnectionError) as error:
                if getattr(error.__cause__, "status_code", None) is not None:
                    raise
                last_error = error
                if method.lower() not in {"get", "head"}:
                    raise ConnectionError("Could not reach Chemotion; the write was not retried.") from error
                if attempt + 1 < TRANSIENT_REQUEST_ATTEMPTS:
                    time.sleep(attempt + 1)
        raise ConnectionError(f"Could not reach Chemotion after {TRANSIENT_REQUEST_ATTEMPTS} attempts: {last_error}") from last_error


class InventorySample(Sample):
    def _clean_properties_data(self, serialize_data=None):
        data = super()._clean_properties_data(serialize_data)
        structure_update = getattr(self, "structure_update", None)
        if structure_update is not None:
            data.pop("molecule", None)
            data.update(structure_update)
        return data

    @property
    def gas_type(self):
        # Inventory edits retain the stored gas settings. The upstream getter
        # loads the linked reaction and can fail on unrelated custom fields.
        return self.properties.get("gas_type", GasType.OFF)

    def _clean_analyses_data(self):
        # The library expects a first analysis container even when the ELN
        # returns an empty children array. Inventory edits leave it empty.
        if as_dict(self.json_data.get("container")).get("children") == []:
            return {}
        return super()._clean_analyses_data()


class ChemotionClient:
    """Inventory operations over a chemotionapi authenticated connection."""
    def __init__(self):
        self.connection = InventoryConnection(CHEMOTION_URL)
        self.instance = Instance(self.connection)

    def request(self, path, data=None, headers=None, method=None):
        """Send exports, chemical metadata, images, and compatibility requests."""
        # Retain the byte-oriented inventory interface. chemotionapi serializes
        # non-string JSON data itself, so decode our already serialized bodies.
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        verb = (method or ("POST" if data is not None else "GET")).lower()
        if verb not in {"get", "post", "put", "patch", "delete"}:
            raise ValueError(f"Unsupported Chemotion request method: {verb}")
        with getattr(self.connection, verb)(path, data=data,
                                            headers=headers or {"Accept": "application/json"}) as response:
            return response.content

    def login(self):
        global _authentication_key, _authentication_token
        # Share only the token; each worker retains its own requests session.
        key = (CHEMOTION_URL, CHEMOTION_USERNAME, CHEMOTION_PASSWORD)
        with _authentication_lock:
            if key != _authentication_key:
                _authentication_key = None
                _authentication_token = None
            if _authentication_token is None:
                self.instance.login(CHEMOTION_USERNAME, CHEMOTION_PASSWORD)
                _authentication_key = key
                _authentication_token = self.connection.bearer_token
            else:
                self.connection.set_bearer_token(_authentication_token)
    def export_collection(self, collection, destination, timeout=900, queued=None):
        """Wait for a new export notification and save its ZIP atomically."""
        def messages():
            payload = json.loads(self.request("/api/v1/messages/list?is_ack=9"))
            return payload.get("messages") or []

        previous = {str(item.get("message_id", item.get("id"))) for item in messages()}
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        try:
            self.request("/api/v1/collections/export",
                         json.dumps({"collection_ids": [collection["id"]]}).encode(),
                         headers, method="POST")
        except ConnectionError as error:
            # Chemotion 2.x/3.x uses the plural route and different parameters.
            if getattr(error.__cause__, "status_code", None) != 404:
                raise
            self.request("/api/v1/collections/exports",
                         json.dumps({"collections": [collection["id"]], "format": "zip", "nested": False}).encode(),
                         headers, method="POST")
        if queued:
            queued()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for item in messages():
                if str(item.get("message_id", item.get("id"))) in previous:
                    continue
                content = as_dict(item.get("content"))
                data = str(content.get("data") or "")
                if str(collection["label"]) not in data or "Export" not in data:
                    continue
                if item.get("subject") == "Collection Import and Export Failure":
                    raise ConnectionError("Chemotion could not export this collection.")
                if item.get("subject") != "Collection Import and Export":
                    continue
                link = content.get("url") or ""
                parsed = urlsplit(link)
                server = urlsplit(CHEMOTION_URL)
                if (parsed.netloc and (parsed.scheme, parsed.netloc) != (server.scheme, server.netloc)) or not re.fullmatch(r"/zip/[\w-]+\.zip", parsed.path):
                    raise ConnectionError("Chemotion returned an unexpected export download link.")
                archive = self.request(link)
                if not zipfile.is_zipfile(io.BytesIO(archive)):
                    raise ConnectionError("Chemotion did not return a ZIP archive.")
                destination = Path(destination)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as output:
                        temporary = Path(output.name)
                        output.write(archive)
                    temporary.replace(destination)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                return
            time.sleep(3)
        raise TimeoutError("The export is still pending. Check Chemotion notifications for its download link.")

    def collections(self, include_parent_collections=False):
        """Return selectable owned and shared collections."""
        root = self.instance.get_root_collection()
        records = [(root.all, False)]
        records.extend((collection.to_json(), False) for collection in root.children)
        records.extend((collection.to_json(), True) for collection in root.sync_root.children)
        found = {}
        def add(collection, synchronized):
            for child in collection.get("children") or []:
                add(child, synchronized)
            if collection.get("id") is not None and (include_parent_collections or not collection.get("children")):
                found[(collection["id"], synchronized)] = {**collection, "_synchronized": synchronized}
        for record, synchronized in records: add(record, synchronized)
        return sorted(found.values(), key=lambda item: str(item.get("label") or "").casefold())

    @staticmethod
    def related_label_matcher(labels, descendants):
        labels = {label for label in labels if label}
        parents = set()
        if descendants is not True:
            for label in labels:
                while re.search(r"-[0-9]+-[0-9]+$", label):
                    label = label.rsplit("-", 1)[0]
                    parents.add(label)
        if descendants is False or not labels:
            return parents.__contains__ if parents else None
        suffix = re.compile(r"(?:-[0-9]+)+$")
        return lambda label: label in parents or any(
            label.startswith(parent) and suffix.fullmatch(label[len(parent):]) for parent in labels)

    def related_samples(self, labels, existing_ids, descendants, progress=None, cancelled=None, rows_loaded=None,
                        sample_list=None, sample_list_loaded=None):
        """Find split relatives in accessible collections using Chemotion short labels."""
        matches = self.related_label_matcher(labels, descendants)
        if matches is None and (sample_list is not None or sample_list_loaded is None):
            return []

        if sample_list is None:
            self.login()
            collections = [
                collection for collection in self.collections(include_parent_collections=True)
                if collection.get("_synchronized")
                or (collection.get("label") == "All" and collection.get("is_locked"))
            ]
            pages = None
        else:
            collections, pages = sample_list
        return self.inventory(
            None, collections=collections, progress=progress, cancelled=cancelled,
            rows_loaded=rows_loaded, sample_filter=lambda sample: (
                matches is not None and sample["id"] not in existing_ids and matches(sample.get("short_label") or "")),
            sample_pages=pages, sample_list_loaded=sample_list_loaded)

    def inventory(self, collection_id, synchronized=False, progress=None, cancelled=None, rows_loaded=None, sequential=False, row_ready=None, collections=None, newest_first=True, sample_filter=None, sample_pages=None, sample_list_loaded=None):
        """Retrieve the inventory, reporting progress through ``progress``."""
        stopped = threading.Event()
        rows = []
        def check_cancelled():
            if stopped.is_set() or (cancelled and cancelled.is_set()):
                raise InventoryLoadCancelled([row for row in rows if row is not None])

        check_cancelled()
        selected = collections if collections is not None else [{"id": collection_id, "_synchronized": synchronized}]

        def collection_name(item):
            label = item.get("label") or item.get("name")
            if label == "All" and item.get("is_locked") and not item.get("_synchronized"):
                return None
            return label

        collection_labels = [collection_name(item) for item in selected]
        collection_word = "collection" if len(selected) == 1 else "collections"
        if progress: progress(f"Retrieving {collection_word}: 0 of {len(selected)}…")
        self.login(); check_cancelled(); samples = []
        sample_contexts = {}
        # The listing thread replaces immutable tuples so workers can read a
        # snapshot while more collection pages are still arriving.
        sample_collection_names = {}
        list_worker_state = threading.local()

        def set_collection_display(row):
            names = sample_collection_names.get(row["id"], ())
            if names:
                row["collection"] = ", ".join(sorted(names, key=str.casefold))

        def retrieve_page(collection, page):
            check_cancelled()
            client = getattr(list_worker_state, "client", None)
            if client is None:
                client = ChemotionClient()
                client.login()
                list_worker_state.client = client
            check_cancelled()
            context_id = collection["id"]
            if page == 1:
                # Use ElementSet to discover pagination without loading details.
                elements = ElementSet(client.connection, {"name": "sample", "is_generic": False},
                                      context_id)
                elements.load_elements(per_page=100)
                return [sample.json_data for sample in list.__iter__(elements)], max(1, elements.max_page)
            payload = {"page": page, "per_page": 100, "filter_created_at": False,
                       "product_only": False, "el_type": "sample",
                       "collection_id": context_id}
            with client.connection.get("/api/v1/samples.json", data=payload) as response:
                return response.json().get("samples", []), None

        listing_queue = Queue()
        listing_finished = threading.Event()
        discovered_count = 0

        def retrieve_lists():
            nonlocal discovered_count
            # Lists keep advancing independently while the consumer loads details.
            workers = 1 if sequential else MAX_CONCURRENT_LIST_REQUESTS
            executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sample-list")
            pending = {}
            page_samples = {}
            saved_pages = {} if sample_list_loaded is not None and sample_pages is None else None
            page_counts = {}
            remaining_pages = {}
            completed_collections = 0
            next_collection, next_page = 0, 1
            jobs = deque((index, 1) for index in range(len(selected)))
            try:
                while pending or jobs:
                    check_cancelled()
                    while len(pending) < workers and jobs:
                        check_cancelled()
                        index, page = jobs.popleft()
                        if sample_pages is None:
                            future = executor.submit(retrieve_page, selected[index], page)
                        else:
                            future = executor.submit(sample_pages.__getitem__, (index, page))
                        pending[future] = (index, page)
                    done, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                    for future in done:
                        index, page = pending.pop(future)
                        batch, max_page = future.result()
                        if saved_pages is not None:
                            saved_pages[index, page] = (batch, max_page)
                        page_samples[index, page] = batch
                        if page == 1:
                            page_counts[index] = max_page
                            remaining_pages[index] = max_page
                            jobs.extend((index, number) for number in range(2, max_page + 1))
                        remaining_pages[index] -= 1
                        if remaining_pages[index] == 0:
                            completed_collections += 1
                            if progress:
                                progress(f"Retrieving {collection_word}: {completed_collections} of {len(selected)}…")
                    # Release pages in selection order to keep duplicate samples
                    # attached to the first selected collection, regardless of timing.
                    ready_batches = []
                    while (next_collection, next_page) in page_samples:
                        collection = selected[next_collection]
                        label = collection_labels[next_collection]
                        batch = []
                        for sample in page_samples.pop((next_collection, next_page)):
                            if sample_filter is not None and not sample_filter(sample):
                                continue
                            sample_id = sample["id"]
                            if sample_id not in sample_contexts:
                                sample_contexts[sample_id] = collection
                                batch.append(sample)
                            if label:
                                names = sample_collection_names.get(sample_id, ())
                                if label not in names:
                                    sample_collection_names[sample_id] = (*names, label)
                        discovered_count += len(batch)
                        if batch:
                            ready_batches.append(batch)
                        if next_page == page_counts[next_collection]:
                            next_collection += 1
                            next_page = 1
                        else:
                            next_page += 1
                    if not pending and not jobs:
                        listing_finished.set()
                    for batch in ready_batches:
                        listing_queue.put(batch)
                if saved_pages is not None:
                    check_cancelled()
                    sample_list_loaded((selected, saved_pages))
            except Exception as error:
                listing_queue.put(error)
            finally:
                executor.shutdown(wait=not (stopped.is_set() or (cancelled and cancelled.is_set())), cancel_futures=True)
                listing_finished.set()
                listing_queue.put(None)

        # The compact list intentionally omits physical location and amount fields.
        # Fetch details for available pages while later lists are still arriving.
        # Each worker owns its authenticated chemotionapi session; mutable
        # sessions must not be shared between threads.
        positions = {}
        sample_details = {}
        worker_state = threading.local()
        def retrieve_sample(sample):
            check_cancelled()
            client = getattr(worker_state, "client", None)
            if client is None:
                client = ChemotionClient(); client.login(); worker_state.client = client
            check_cancelled()
            collection = sample_contexts[sample["id"]]
            row = client.inventory_entry(sample["id"], collection["id"], bool(collection.get("_synchronized")),
                                         sample, sample_details.get(sample["id"]))
            set_collection_display(row)
            row["_inventory_position"] = positions[sample["id"]]
            # Retain retrieved details even if Stop interrupts thumbnail display.
            rows[positions[sample["id"]]] = row
            if row_ready:
                row_ready([row])
            return row

        workers = 1 if sequential else MAX_CONCURRENT_SAMPLE_REQUESTS
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sample-retrieval")
        listing_thread = threading.Thread(target=retrieve_lists, name="sample-list-coordinator", daemon=True)
        listing_thread.start()
        try:
            futures = {}
            next_sample = 0
            batch_end = 0
            completed = 0
            pending = set()
            batch_supported = True
            empty_batch_contexts = set()
            listing_done = False
            while pending or next_sample < len(samples) or not listing_done:
                check_cancelled()
                if not pending and next_sample == len(samples):
                    try:
                        batch = listing_queue.get(timeout=0.1)
                    except Empty:
                        continue
                    if isinstance(batch, Exception):
                        raise batch
                    if batch is None:
                        listing_done = True
                        continue
                    batch.sort(key=lambda sample: natural_sort_value(shown(sample.get("short_label"))), reverse=newest_first)
                    for sample in batch:
                        positions[sample["id"]] = len(samples)
                        samples.append(sample)
                        rows.append(None)
                if not pending and next_sample == batch_end:
                    batch_end = min(next_sample + SAMPLE_DETAIL_BATCH_SIZE, len(samples))
                    sample_details.clear()
                    groups = {}
                    for sample in samples[next_sample:batch_end]:
                        collection = sample_contexts[sample["id"]]
                        groups.setdefault(collection["id"], []).append(sample["id"])
                    for context_id, sample_ids in groups.items():
                        if cancelled and cancelled.is_set():
                            raise InventoryLoadCancelled([row for row in rows if row is not None])
                        if batch_supported and context_id not in empty_batch_contexts:
                            try:
                                details = self.sample_details(sample_ids, context_id)
                                sample_details.update(details)
                                # Some servers list shared samples but exclude
                                # them from this route. Keep individual reads.
                                if not details:
                                    empty_batch_contexts.add(context_id)
                            except ConnectionError as error:
                                if getattr(error.__cause__, "status_code", None) not in {404, 405}:
                                    raise
                                # Older servers may not expose the batch route.
                                batch_supported = False
                if not pending:
                    submit_end = next_sample + 1 if sequential else batch_end
                    for position in range(next_sample, submit_end):
                        future = executor.submit(retrieve_sample, samples[position])
                        futures[future] = position
                        pending.add(future)
                    next_sample = submit_end
                done, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                batch_rows = []
                for future in done:
                    try:
                        rows[futures[future]] = future.result()
                    except InventoryLoadCancelled:
                        raise InventoryLoadCancelled([row for row in rows if row is not None])
                    batch_rows.append(rows[futures[future]])
                    completed += 1
                    if progress and listing_finished.is_set():
                        progress(f"Retrieving sample information: {completed} of {discovered_count}…")
                if batch_rows and rows_loaded:
                    rows_loaded(batch_rows)
                if cancelled and cancelled.is_set():
                    raise InventoryLoadCancelled([row for row in rows if row is not None])
        except InventoryLoadCancelled as interrupted:
            for row in interrupted.rows:
                set_collection_display(row)
            raise
        finally:
            stopped.set()
            executor.shutdown(wait=not (cancelled and cancelled.is_set()), cancel_futures=True)
            listing_thread.join(timeout=0.3)
        # Later pages can contain labels preceding those already shown. The GUI
        # sorts visible rows; assign final positions once discovery is complete.
        samples.sort(key=lambda sample: natural_sort_value(shown(sample.get("short_label"))), reverse=newest_first)
        ordered_rows = [rows[positions[sample["id"]]] for sample in samples]
        for position, row in enumerate(ordered_rows):
            set_collection_display(row)
            row["_inventory_position"] = position
        return ordered_rows
    def sample_details(self, sample_ids, collection_id):
        """Fetch up to 100 full sample records through the read-only batch route."""
        if not sample_ids:
            return {}
        if len(sample_ids) > SAMPLE_DETAIL_BATCH_SIZE:
            raise ValueError("Request at most 100 sample details per batch.")
        payload = {"ui_state": {"collection_id": collection_id,
                                "all": False,
                                "included_ids": sample_ids, "excluded_ids": []},
                   "limit": len(sample_ids)}
        response = json.loads(self.request(
            "/api/v1/samples/ui_state", json.dumps(payload).encode(),
            {"Accept": "application/json", "Content-Type": "application/json"}, method="POST"))
        records = response.get("samples")
        if not isinstance(records, list):
            raise ConnectionError("Chemotion returned an invalid sample batch.")
        requested = set(sample_ids)
        return {sample["id"]: sample for sample in records
                if isinstance(sample, dict) and sample.get("id") in requested}

    def inventory_entry(self, sample_id, collection_id=None, synchronized=False, fallback_sample=None, detail=None):
        """Retrieve the complete, current display record for one sample."""
        if detail is None:
            detail = self.instance.get_sample(sample_id).json_data
        chemical = json.loads(self.request(f"/api/v1/chemicals?sample_id={sample_id}").decode("utf-8"))
        row = inventory_row(detail, inventory_vendor(chemical), collection_id, synchronized)
        row["chemical_data"] = as_dict(chemical).get("chemical_data") or []
        row["chemical_id"] = as_dict(chemical).get("id")
        row["chemical_cas"] = chemical_cas(chemical, detail)
        return row
    def set_location(self, sample_id, location, container, decoupled, inventory_sample):
        """Set one location while preserving flags defaulted by Chemotion's API."""
        self._save_sample(sample_id, {"location": location})

    @contextmanager
    def _sample_write(self, sample_id):
        try:
            yield
        except ConnectionError as error:
            rejection = error.__cause__
            if (getattr(rejection, "status_code", None) in {401, 403}
                    and getattr(rejection, "method", None) in {"POST", "PUT"}
                    and self._sample_write_denied(sample_id)):
                raise PermissionError("The signed-in account does not have write permission for this sample.") from error
            raise

    def _sample_write_denied(self, sample_id):
        """Check the signed-in account's sample policy after a rejected write."""
        try:
            payload = json.loads(self.request(f"/api/v1/samples/{sample_id}.json"))
        except (ConnectionError, ValueError):
            return False
        return as_dict(as_dict(payload).get("sample")).get("can_update") is False

    def set_amount(self, sample_id, field, value, unit, container, decoupled, inventory_sample):
        """Update one amount while preserving the sample's other editable flags."""
        self._save_sample(sample_id, {field: {"value": str(value), "unit": unit}})
    def set_density(self, sample_id, density, container, decoupled, inventory_sample):
        """Update density while retaining the current container and sample flags."""
        self._save_sample(sample_id, {"density": density})
    def set_sample_flags(self, sample_id, container, decoupled, inventory_sample):
        """Update the two sample flags together, retaining the current container."""
        self._save_sample(sample_id, {"decoupled": decoupled, "inventory_sample": inventory_sample})

    def get_sample_structure(self, sample_id):
        sample = self.instance.get_sample(sample_id).json_data
        return self._sample_structure(sample)

    @staticmethod
    def _sample_structure(sample):
        molecule = as_dict(sample.get("molecule"))
        molfile = sample.get("molfile") or molecule.get("molfile")
        if not molecule.get("id") or not isinstance(molfile, str) or not molfile.strip():
            raise ValueError("This sample has no accessible molecular structure.")
        name = as_dict(sample.get("molecule_name_hash"))
        display_name = (name.get("label") or sample.get("showed_name") or molecule.get("iupac_name")
                        or molecule.get("sum_formular") or "Unnamed molecule")
        return {
            "molfile": molfile,
            "molecule_id": molecule["id"],
            "molecule_name_id": name.get("value"),
        }, display_name

    def set_sample_structure(self, sample_id, structure, properties=None):
        # The ELN consumes the temporary SVG during the sample update, so each
        # destination gets its own file and thumbnail URL.
        filename = f"TMPFILE{secrets.token_hex(32)}.svg"
        payload = {"svgs": [{"svg_path": filename, "molfile": structure["molfile"]}]}
        response = json.loads(self.request(
            "/api/v1/samples/batch-refresh-svg", json.dumps(payload).encode(),
            {"Accept": "application/json", "Content-Type": "application/json"}, method="POST"))
        results = response.get("results") or [{}]
        result = results[0]
        if not result.get("success") or result.get("filename") != filename:
            raise ConnectionError(result.get("error") or "Chemotion could not generate the sample SVG.")
        properties = dict(properties or {})
        structure_update = {**structure, "sample_svg_file": filename}
        # Copy purity in the sample payload without recalculating reaction quantities.
        if "purity" in properties:
            structure_update["purity"] = properties.pop("purity")
        self._save_sample(sample_id, properties, structure_update=structure_update)
        return self.instance.get_sample(sample_id).json_data

    def get_sample_copy_properties(self, sample_id):
        source = self.instance.get_sample(sample_id)
        detail = source.json_data
        structure, _ = self._sample_structure(detail)
        fields = (
            "name", "description", "external_label", "location", "density", "purity",
            "solvent", "dry_solvent", "stereo", "molarity", "molecular_mass", "sum_formula",
            "boiling_point_lowerbound", "boiling_point_upperbound", "melting_point_lowerbound",
            "melting_point_upperbound", "decoupled", "inventory_sample",
        )
        properties = {key: source.properties[key] for key in fields if key in source.properties}
        for key in ("dry_solvent", "impurities"):
            if key in detail:
                properties[key] = detail[key]
        if not properties.get("location"):
            properties["location"] = as_dict(detail.get("container")).get("name") or ""
        chemical = self.inventory_entry(sample_id, detail=detail)
        vendor = inventory_vendor({"chemical_data": chemical["chemical_data"]})
        return structure, properties, vendor or detail.get("vendor") or detail.get("supplier") or ""

    def copy_sample_properties(self, row, structure, properties, vendor):
        sample_id = row["id"]
        detail = self.set_sample_structure(sample_id, structure, properties)
        if (as_dict(detail.get("molecule")).get("id") != structure["molecule_id"]
                or detail.get("molfile") != structure["molfile"] or not detail.get("sample_svg_file")):
            raise ConnectionError("Chemotion did not retain the copied structure or its SVG.")
        chemical = self.inventory_entry(
            sample_id, row.get("collection_id"), row.get("synchronized", False), detail=detail)
        if chemical["vendor"] != shown(vendor):
            self.set_vendor(sample_id, chemical["chemical_data"], vendor,
                            chemical["chemical_id"], chemical["chemical_cas"])

    def _save_sample(self, sample_id, properties, molecule_name_id=None, structure_update=None):
        # Fetch current data before saving: GUI rows may be stale and save()
        # serializes the complete sample, including its container and flags.
        sample = self.instance.get_sample(sample_id)
        if isinstance(sample, Sample):
            sample = InventorySample(GenericSegments(self.connection), self.connection,
                                     json_data=sample.json_data, element_type="sample")
            # This is a full get_sample response, not a partial listing record.
            sample._data_loaded = True
        if structure_update is not None:
            sample.structure_update = structure_update
        for key, value in properties.items():
            sample.properties[key] = value
        if molecule_name_id is not None:
            sample.json_data["molecule_name_id"] = molecule_name_id
        # chemotionapi normally copies the molecule molfile into the sample.
        # Inventory edits must retain the sample's own structure, even if null.
        if "molfile" in sample.json_data:
            sample.molecule["molfile"] = sample.json_data["molfile"]
        with self._sample_write(sample_id):
            sample.save()
    def remove_sample_from_collection(self, collection_id, sample_id, synchronized=False):
        """Remove one sample's membership in a collection without deleting it."""
        root = self.instance.get_root_collection(reload=False)
        tree = root.sync_root if synchronized else root
        matches = tree.find(id=collection_id)
        if not matches:
            raise ConnectionError("The collection is no longer available. Reload the collection list.")
        # A listing record identifies an existing sample without fetching it.
        elements = ElementSet(self.connection, {"name": "sample", "is_generic": False})
        sample = elements.new_element({"id": sample_id, "type": "sample", "molecule": {}})
        return {"locked_sample_ids": matches[0].remove_elements(sample)}
    def set_sample_name(self, sample_id, name, container, decoupled, inventory_sample):
        """Update a sample name without letting API defaults reset its flags."""
        self._save_sample(sample_id, {"name": name})
    def create_molecule_name(self, molecule_id, name):
        """Create a molecule-name option and return Chemotion's identifier for it."""
        query = urlencode({"id": molecule_id, "new_name": name})
        names = json.loads(self.request(f"/api/v1/molecules/names?{query}").decode("utf-8")).get("molecules") or []
        match = next((item for item in names if item.get("label") == name), None)
        if not match or match.get("value") is None: raise ConnectionError("Chemotion did not return the new molecule name.")
        return match["value"]
    def set_molecule_name(self, sample_id, molecule_name_id, container, decoupled, inventory_sample):
        """Select a molecule name for one sample while preserving its flags."""
        self._save_sample(sample_id, {}, molecule_name_id=molecule_name_id)
    def set_vendor(self, sample_id, chemical_data, vendor, chemical_id=None, cas=""):
        """Set a vendor, creating Chemotion's chemical record when it is absent."""
        data = json.loads(json.dumps(chemical_data)) if chemical_data else [{}]
        if not isinstance(data[0], dict): data[0] = {}
        data[0]["vendor"] = vendor
        payload = {"sample_id": sample_id, "chemical_data": data}
        path = "/api/v1/chemicals"
        method = "PUT"
        if chemical_id is None:
            # GET /chemicals returns an unsaved object (id: null) for entries
            # such as iNV-173 that do not yet have any inventory metadata.
            # Such an object must be created, not updated.
            path = "/api/v1/chemicals/create"
            payload["cas"] = cas or ""
            method = "POST"
        body = json.dumps(payload).encode("utf-8")
        with self._sample_write(sample_id):
            self.request(path, body,
                         {"Accept": "application/json", "Content-Type": "application/json"}, method=method)

