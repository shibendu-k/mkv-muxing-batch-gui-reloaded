import cgi
import json
import os
import shutil
import subprocess
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


DATA_ROOT = Path(os.environ.get("DATA_ROOT", "/data")).resolve()
WEB_ROOT = Path(__file__).resolve().parent
TOOLS = {
    name: shutil.which(name)
    for name in ("mkvmerge", "mkvextract", "mkvinfo", "mkvpropedit")
}
DATA_ROOT.mkdir(parents=True, exist_ok=True)
JOBS = {}
JOBS_LOCK = threading.Lock()


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def safe_path(value):
    if not isinstance(value, str) or not value:
        raise ValueError("A file path is required")
    path = Path(value)
    if not path.is_absolute():
        path = DATA_ROOT / path
    path = path.resolve()
    if path != DATA_ROOT and DATA_ROOT not in path.parents:
        raise ValueError("Paths must stay inside /data")
    return path


def read_json(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    if not length:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def tool_version(name):
    path = TOOLS.get(name)
    if not path:
        return None
    try:
        result = subprocess.run(
            [path, "-V"], capture_output=True, text=True, timeout=10, check=False
        )
        return (result.stdout or result.stderr).strip()
    except (OSError, subprocess.TimeoutExpired):
        return None


def append_output_path(args, output):
    output_path = safe_path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    args.extend(["-o", str(output_path)])
    return output_path


def start_job(tool, args):
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "tool": tool,
            "status": "queued",
            "args": args,
            "stdout": "",
            "stderr": "",
            "returncode": None,
        }

    def worker():
        try:
            if not TOOLS.get(tool):
                raise FileNotFoundError(f"{tool} is not installed")
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "running"
            result = subprocess.run(
                [TOOLS[tool], *args],
                capture_output=True,
                text=True,
                timeout=24 * 60 * 60,
                check=False,
            )
            with JOBS_LOCK:
                JOBS[job_id].update(
                    status="completed" if result.returncode == 0 else "failed",
                    stdout=result.stdout,
                    stderr=result.stderr,
                    returncode=result.returncode,
                )
        except Exception as exc:
            with JOBS_LOCK:
                JOBS[job_id].update(status="failed", stderr=str(exc), returncode=-1)

    threading.Thread(target=worker, name=f"mkv-{job_id}", daemon=True).start()
    return job_id


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "MKVDashboard/1.0"

    def send_json(self, status, value):
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message):
        self.send_json(status, {"error": message})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self.send_file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
            return
        if path == "/app.css":
            self.send_file(WEB_ROOT / "app.css", "text/css; charset=utf-8")
            return
        if path == "/app.js":
            self.send_file(WEB_ROOT / "app.js", "application/javascript; charset=utf-8")
            return
        if path == "/api/status":
            self.send_json(
                HTTPStatus.OK,
                {
                    "data_root": str(DATA_ROOT),
                    "tools": {
                        name: {"path": path, "version": tool_version(name)}
                        for name, path in TOOLS.items()
                    },
                },
            )
            return
        if path == "/api/files":
            self.send_json(HTTPStatus.OK, {"files": list_files()})
            return
        if path == "/api/jobs":
            with JOBS_LOCK:
                values = [dict(job) for job in reversed(list(JOBS.values()))]
            self.send_json(HTTPStatus.OK, {"jobs": values})
            return
        if path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                self.send_error_json(HTTPStatus.NOT_FOUND, "Job not found")
            else:
                self.send_json(HTTPStatus.OK, {"job": dict(job)})
            return
        if path == "/api/download":
            query = parse_qs(urlparse(self.path).query)
            try:
                file_path = safe_path(query["path"][0])
            except (KeyError, ValueError) as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                return
            if not file_path.is_file():
                self.send_error_json(HTTPStatus.NOT_FOUND, "File not found")
                return
            self.send_file(file_path, "application/octet-stream", download=True)
            return
        if path == "/api/info":
            query = parse_qs(urlparse(self.path).query)
            try:
                file_path = safe_path(query["path"][0])
            except (KeyError, ValueError) as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                return
            if not file_path.is_file():
                self.send_error_json(HTTPStatus.NOT_FOUND, "File not found")
                return
            try:
                result = subprocess.run(
                    [TOOLS["mkvmerge"], "-J", str(file_path)],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                if result.returncode == 0:
                    value = json.loads(result.stdout)
                else:
                    value = {"error": result.stderr or "MKV identification failed"}
                self.send_json(HTTPStatus.OK, value)
            except Exception as exc:
                self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return

        self.send_error_json(HTTPStatus.NOT_FOUND, "Endpoint not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/upload":
            self.handle_upload()
            return
        try:
            body = read_json(self)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, f"Invalid JSON: {exc}")
            return

        try:
            if parsed.path == "/api/run":
                job_id = handle_raw_run(body)
            elif parsed.path == "/api/mux":
                job_id = handle_mux(body)
            elif parsed.path == "/api/extract":
                job_id = handle_extract(body)
            elif parsed.path == "/api/propedit":
                job_id = handle_propedit(body)
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "Endpoint not found")
                return
            self.send_json(HTTPStatus.ACCEPTED, {"job_id": job_id})
        except (ValueError, OSError) as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

    def handle_upload(self):
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers.get("Content-Type")},
            keep_blank_values=True,
        )
        uploaded = form["file"]
        if not getattr(uploaded, "filename", ""):
            self.send_error_json(HTTPStatus.BAD_REQUEST, "No file selected")
            return
        filename = Path(uploaded.filename).name
        destination = DATA_ROOT / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as output:
            shutil.copyfileobj(uploaded.file, output)
        self.send_json(HTTPStatus.CREATED, {"path": str(destination)})

    def send_file(self, path, content_type, download=False):
        if not path.is_file():
            self.send_error_json(HTTPStatus.NOT_FOUND, "File not found")
            return
        with path.open("rb") as file:
            body = file.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if download:
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{path.name.replace(chr(34), "_")}"',
            )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string, *args):
        print("%s - %s" % (self.address_string(), format_string % args))


def list_files():
    result = []
    for path in sorted(DATA_ROOT.rglob("*")):
        try:
            relative = path.relative_to(DATA_ROOT)
        except ValueError:
            continue
        result.append(
            {
                "path": str(path),
                "relative": str(relative),
                "name": path.name,
                "type": "directory" if path.is_dir() else "file",
                "size": path.stat().st_size if path.is_file() else None,
            }
        )
    return result


def validate_args(args):
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError("args must be an array of strings")
    if any("\x00" in arg for arg in args):
        raise ValueError("Invalid argument")


def handle_raw_run(body):
    tool = body.get("tool")
    if tool not in TOOLS:
        raise ValueError("tool must be mkvmerge, mkvextract, mkvinfo or mkvpropedit")
    args = body.get("args", [])
    validate_args(args)
    for arg in args:
        if "/" in arg or "\\" in arg:
            for candidate in arg.replace("\\", "/").split(":"):
                if candidate.startswith("/"):
                    safe_path(candidate)
    return start_job(tool, args)


def handle_mux(body):
    sources = body.get("sources", [])
    if not isinstance(sources, list) or not sources:
        raise ValueError("At least one source file is required")
    output = body.get("output")
    args = []
    append_output_path(args, output)
    title = body.get("title")
    if title:
        args.extend(["--title", str(title)])
    attachments = body.get("attachments", [])
    if not isinstance(attachments, list):
        raise ValueError("attachments must be an array")
    for attachment in attachments:
        path = safe_path(attachment)
        if not path.is_file():
            raise ValueError(f"Attachment not found: {attachment}")
        args.extend(["--attachment-name", path.name, "--attach-file", str(path)])
    extra_args = body.get("args", [])
    validate_args(extra_args)
    args.extend(extra_args)
    for source in sources:
        path = safe_path(source)
        if not path.is_file():
            raise ValueError(f"Source not found: {source}")
        args.append(str(path))
    return start_job("mkvmerge", args)


def handle_extract(body):
    source = safe_path(body.get("source"))
    if not source.is_file():
        raise ValueError(f"Source not found: {body.get('source')}")
    mode = body.get("mode", "tracks")
    destination = body.get("destination")
    if mode == "tracks":
        items = body.get("items", [])
        if not isinstance(items, list) or not items:
            raise ValueError("At least one track extraction item is required")
        args = ["tracks", str(source)]
        for item in items:
            track_id = item.get("id")
            track_destination = safe_path(item.get("destination") or destination)
            track_destination.parent.mkdir(parents=True, exist_ok=True)
            args.append(f"{track_id}:{track_destination}")
        return start_job("mkvextract", args)

    allowed_modes = {
        "attachments", "chapters", "tags", "timestamps_v2",
        "cuesheet", "cues", "cues_v2",
    }
    if mode not in allowed_modes:
        raise ValueError("Unsupported extraction mode")
    args = [mode, str(source)]
    extra_args = body.get("args", [])
    validate_args(extra_args)
    args.extend(extra_args)
    if destination:
        args.append(str(safe_path(destination)))
    return start_job("mkvextract", args)


def handle_propedit(body):
    target = safe_path(body.get("file"))
    if not target.is_file():
        raise ValueError(f"File not found: {body.get('file')}")
    args = [str(target)]
    edits = body.get("edits", [])
    if not isinstance(edits, list):
        raise ValueError("edits must be an array")
    for edit in edits:
        selector = edit.get("edit")
        if selector:
            args.extend(["--edit", str(selector)])
        for set_value in edit.get("set", []):
            args.extend(["--set", str(set_value)])
        for delete_value in edit.get("delete", []):
            args.extend(["--delete", str(delete_value)])
        for add_value in edit.get("add", []):
            args.extend(["--add", str(add_value)])
    extra_args = body.get("args", [])
    validate_args(extra_args)
    args.extend(extra_args)
    return start_job("mkvpropedit", args)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8080), DashboardHandler)
    print("MKV Muxing Dashboard listening on http://0.0.0.0:8080", flush=True)
    server.serve_forever()
