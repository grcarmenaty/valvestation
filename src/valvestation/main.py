"""
ValveStation: an HTTP service to control the Canonada projects

Configuration is read from config.toml in the working directory. Install a station with:
    valvestation install
Run with:
    valvestation
"""

import hmac
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import tomllib
import zipfile
import zlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import IO, cast

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

CONFIG_PATH = Path("config.toml").resolve()
PROJECTS_DIR = CONFIG_PATH.parent / "projects"  # One sub-directory per Canonada project
LOGS_DIR = PROJECTS_DIR / ".logs"  # Hidden so it is not listed as a project

log = logging.getLogger("valvestation")

projects_lock = threading.Lock()  # Serialises adding and removing projects
runs_lock = threading.Lock()  # Serialises the run list
runs: list[dict] = []  # Pipeline and system runs for this server process
config: dict  # Filled by main() before the server accepts requests

app = FastAPI()


def install() -> None:
    """
    Copy the packaged template config.toml into the working directory and create projects/.
    An existing config.toml is left unchanged.
    """

    dest = Path("config.toml").resolve()
    if dest.is_file():
        log.info(f"Config already present at {dest}")
    else:
        template = Path(__file__).resolve().parent / "templates" / "config.toml"
        if not template.is_file():
            log.error(f"No config template found at {template}")
            sys.exit(1)
        shutil.copyfile(template, dest)
        log.info(f"Wrote config to {dest}. Set 'token' in it before starting")

    (dest.parent / "projects").mkdir(exist_ok=True)


def check_install() -> None:
    """
    ValveStation runs from a directory holding config.toml and a projects directory. Logs an error
    and exits if either is missing.
    """

    missing = False
    if not CONFIG_PATH.is_file():
        log.error(f"No config found at {CONFIG_PATH}")
        missing = True
    if not PROJECTS_DIR.is_dir():
        log.error(f"No projects directory found at {PROJECTS_DIR}")
        missing = True
    if missing:
        sys.exit(1)


def load_config() -> dict:
    """
    Read config.toml. Logs an error and exits if the file can't be parsed, or if a mandatory
    setting fails its check.

    token must be a non-empty string: the pre-shared bearer token.
    canonada_timeout must be a positive int or float: seconds Canonada may take to load a
    project. A bool is rejected, because bool is a subclass of int.
    """

    try:
        with CONFIG_PATH.open("rb") as f:
            config = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.error(f"Could not read {CONFIG_PATH}: {e}")
        sys.exit(1)

    # Check mandatory settings
    # -- token
    if not isinstance(config.get("token"), str) or not config["token"]:
        log.error(f"Set 'token' in {CONFIG_PATH}")
        sys.exit(1)

    # -- canonada_timeout
    timeout = config.get("canonada_timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        log.error(f"Set 'canonada_timeout' in {CONFIG_PATH} to a positive number of seconds")
        sys.exit(1)

    return config

@app.middleware("http")
async def check_token(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """
    Every request needs the pre-shared token. It's checked before the body is read, so
    unauthenticated uploads are never stored.
    """

    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), config["token"].encode()):
        return JSONResponse({"detail": "Invalid or missing token"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
    return await call_next(request)


def _is_project(path: Path) -> bool:
    """
    Whether Canonada can load the directory as a project
    """

    try:
        result = subprocess.run(
            # -P: like the canonada command, the project doesn't shadow installed modules
            [sys.executable, "-P", "-m", "canonada.cli", "registry", "pipelines"],
            cwd=path,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=config["canonada_timeout"],
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def _extract(archive: Path, dest: Path) -> None:
    """
    Unpack a zip or tar archive into dest, refusing members that would land outside of it
    """

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            root = dest.resolve()
            for name in zf.namelist():
                if not (root / name).resolve().is_relative_to(root):
                    raise ValueError(f"Member '{name}' points outside the archive")
            zf.extractall(dest)
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive) as tf:
            tf.extractall(dest, filter="data")
    else:
        raise ValueError("Not a zip or tar archive")

    if not (dest / "canonada.toml").is_file():
        raise HTTPException(400, "The archive does not contain a Canonada project (no canonada.toml at its root)")
    if not _is_project(dest):
        raise HTTPException(400, "Canonada can't load the uploaded project")


def _project_name(project: Path) -> str:
    """
    Extract the name of a Canonada project given its directory path
    """

    with (project / "canonada.toml").open("rb") as f:
        name = tomllib.load(f).get("project", {}).get("name")
    if not isinstance(name, str) or not name or name != Path(name).name:
        raise HTTPException(400, "canonada.toml needs a [project] name that is a single directory under projects/")
    if name.startswith("."):
        raise HTTPException(400, "canonada.toml [project] name must not start with '.'; those directories are uploads and removals in progress")
    return name


# Project ----------------------------------------------------------------------
@app.put("/project/add")
def add_project(file: UploadFile) -> dict:
    """
    Receive a Canonada project as a zip or tar archive. Its name is read from canonada.toml, and a
    project with the same name is overwritten.
    """

    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=PROJECTS_DIR))
    try:
        archive, tree = staging / "upload", staging / "tree"
        with archive.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        tree.mkdir()
        try:
            _extract(archive, tree)
        except (OSError, EOFError, ValueError, RuntimeError, tarfile.TarError, zipfile.BadZipFile, zlib.error) as e:
            raise HTTPException(400, f"Could not unpack the project archive: {e}")

        name = _project_name(tree)

        # Copy the extracted project to the projects dir
        target = PROJECTS_DIR / name
        replaced = False
        # Delete target if exists move tree to target
        with projects_lock:
            if target.exists():
                shutil.rmtree(target)
                replaced = True
            shutil.move(tree, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {"project": name, "replaced": replaced}


@app.delete("/project/remove/{project}")
def remove_project(project: str) -> dict:
    """
    Remove a Canonada project from the station
    """

    # Hidden entries are uploads and removals in progress
    listed = {p.name for p in PROJECTS_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")}
    if project not in listed:
        raise HTTPException(404, f"Project '{project}' not found")

    try:
        with projects_lock:
            shutil.rmtree(PROJECTS_DIR.joinpath(project))
    except FileNotFoundError:  # Removed by another request meanwhile
        raise HTTPException(404, f"Project '{project}' not found")

    return {"project": project, "removed": True}


# Registry ---------------------------------------------------------------------
def _exec_return_json(project: str, script: str) -> list | dict | None:
    """
    Run a script inside a project and return the JSON it prints
    """

    # Hidden entries are uploads and removals in progress
    listed = {p.name for p in PROJECTS_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")}
    if project not in listed:
        raise HTTPException(404, f"Project '{project}' not found")

    try:
        result = subprocess.run(
            [sys.executable, "-P", "-c", script],
            cwd=PROJECTS_DIR / project,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=config["canonada_timeout"],
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, f"Canonada timed out loading project '{project}'")
    except FileNotFoundError:  # Removed by another request meanwhile
        raise HTTPException(404, f"Project '{project}' not found")

    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        reason = f": {detail[-1]}" if detail else ""
        raise HTTPException(500, f"Canonada can't load project '{project}'{reason}")

    try:
        # Importing the project may print; the JSON is the last line
        return json.loads(result.stdout.rsplit("\n", 2)[-2])
    except (json.JSONDecodeError, IndexError):
        raise HTTPException(500, f"Canonada returned no JSON for project '{project}'")


def _expect_dict(value: list | dict | None, project: str) -> dict:
    """
    The JSON object printed by a project script
    """

    if isinstance(value, dict):
        return value
    raise HTTPException(500, f"Canonada returned no JSON for project '{project}'")


def _expect_dicts(value: list | dict | None, project: str) -> list[dict]:
    """
    The JSON list of objects printed by a project script
    """

    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return cast(list[dict], value)
    raise HTTPException(500, f"Canonada returned no JSON for project '{project}'")


def _expect_strs(value: list | dict | None, project: str) -> list[str]:
    """
    The JSON list of strings printed by a project script
    """

    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return cast(list[str], value)
    raise HTTPException(500, f"Canonada returned no JSON for project '{project}'")


@app.get("/registry/projects")
def get_projects() -> dict:
    """
    List available projects
    """
    return {"projects": [p.name for p in PROJECTS_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")]}


@app.get("/registry/projects/{project}/pipelines")
def get_pipelines(project: str) -> list[dict]:
    """
    List a project's pipelines with their description, node names and execution settings
    """

    # Canonada is imported before the project is on the path, so the project can't shadow it.
    script = """\
import json
import os
import sys

from canonada.pipeline import Pipeline

sys.path.append(os.getcwd())
from pipelines import *
from systems import *

entries = [
    {
        "name": p.name,
        "description": p.description,
        "nodes": [node.name for node in p.nodes],
        "max_workers": p.max_workers,
        "multiprocessing": p.multiprocessing,
        "error_tolerant": p.error_tolerant,
    }
    for p in Pipeline.registry
]
sys.stdout.write("\\n")
json.dump(entries, sys.stdout)
sys.stdout.write("\\n")
"""

    return _expect_dicts(_exec_return_json(project, script), project)


@app.get("/registry/projects/{project}/systems")
def get_systems(project: str) -> list[dict]:
    """
    List a project's systems with their description and the pipelines they run, in order
    """

    # Canonada is imported before the project is on the path, so the project can't shadow it.
    script = """\
import json
import os
import sys

from canonada.system import System

sys.path.append(os.getcwd())
from pipelines import *
from systems import *

entries = [
    {
        "name": s.name,
        "description": s.description,
        "pipelines": [p.name for p in s.pipeline],
    }
    for s in System.registry
]
sys.stdout.write("\\n")
json.dump(entries, sys.stdout)
sys.stdout.write("\\n")
"""

    return _expect_dicts(_exec_return_json(project, script), project)


# View -------------------------------------------------------------------------
@app.get("/view/projects/{project}/pipelines/{pipeline}")
def view_pipeline(project: str, pipeline: str) -> dict:
    """
    Returns a pipeline's nodes and their inputs and outputs
    """

    # Canonada is imported before the project is on the path, so the project can't shadow it.
    script = """\
import json
import os
import sys

from canonada.pipeline import Pipeline

sys.path.append(os.getcwd())
from pipelines import *
from systems import *

match = next((p for p in Pipeline.registry if p.name == %%PIPELINE%%), None)
if match is None:
    entry = None
else:
    entry = {
        "name": match.name,
        "description": match.description,
        "nodes": [
            {
                "name": node.name,
                "description": node.description,
                "input": node.input,
                "output": node.output,
            }
            for node in match.nodes
        ],
    }
sys.stdout.write("\\n")
json.dump(entry, sys.stdout)
sys.stdout.write("\\n")
""".replace("%%PIPELINE%%", json.dumps(pipeline))

    view = _exec_return_json(project, script)
    if view is None:
        raise HTTPException(404, f"Pipeline '{pipeline}' not found")
    return _expect_dict(view, project)


@app.get("/view/projects/{project}/systems/{system}")
def view_system(project: str, system: str) -> dict:
    """
    Returns a system's pipelines in run order, each with its nodes and their inputs and outputs
    """

    # Canonada is imported before the project is on the path, so the project can't shadow it.
    script = """\
import json
import os
import sys

from canonada.system import System

sys.path.append(os.getcwd())
from pipelines import *
from systems import *

match = next((s for s in System.registry if s.name == %%SYSTEM%%), None)
if match is None:
    entry = None
else:
    entry = {
        "name": match.name,
        "description": match.description,
        "pipelines": [
            {
                "name": pipe.name,
                "description": pipe.description,
                "nodes": [
                    {
                        "name": node.name,
                        "description": node.description,
                        "input": node.input,
                        "output": node.output,
                    }
                    for node in pipe.nodes
                ],
            }
            for pipe in match.pipeline
        ],
    }
sys.stdout.write("\\n")
json.dump(entry, sys.stdout)
sys.stdout.write("\\n")
""".replace("%%SYSTEM%%", json.dumps(system))

    view = _exec_return_json(project, script)
    if view is None:
        raise HTTPException(404, f"System '{system}' not found")
    return _expect_dict(view, project)

# Catalog ----------------------------------------------------------------------
def _inject_toml(project: str, file: UploadFile, filename: str) -> dict:
    """
    Replace one of a project's config TOML files with an uploaded file
    """

    # Hidden entries are uploads and removals in progress
    listed = {p.name for p in PROJECTS_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")}
    if project not in listed:
        raise HTTPException(404, f"Project '{project}' not found")

    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=PROJECTS_DIR))
    try:
        uploaded = staging / filename
        with uploaded.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        try:
            with uploaded.open("rb") as f:
                tomllib.load(f)
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            raise HTTPException(400, f"Could not read the TOML file: {e}")

        target = PROJECTS_DIR / project / "config" / filename
        with projects_lock:
            listed = {p.name for p in PROJECTS_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")}
            if project not in listed:
                raise HTTPException(404, f"Project '{project}' not found")
            target.parent.mkdir(exist_ok=True)
            replaced = target.is_file()
            shutil.move(uploaded, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {"project": project, "replaced": replaced}


@app.get("/catalog/projects/{project}/catalog")
def get_catalog(project: str) -> list[str]:
    """
    List the datasets in a project's catalog
    """

    script = """\
import json
import sys

from canonada.catalog import ls

try:
    entries = ls()
except FileNotFoundError:
    entries = None
sys.stdout.write("\\n")
json.dump(entries, sys.stdout)
sys.stdout.write("\\n")
"""

    catalog = _exec_return_json(project, script)
    if catalog is None:
        raise HTTPException(404, f"Project '{project}' has no config/catalog.toml")
    return _expect_strs(catalog, project)


@app.get("/catalog/projects/{project}/parameters")
def get_parameters(project: str) -> dict:
    """
    A project's parameters, with nested tables flattened the way Canonada reads them
    """

    script = """\
import json
import sys

from canonada.catalog import params

try:
    entries = params()
except FileNotFoundError:
    entries = None
sys.stdout.write("\\n")
json.dump(entries, sys.stdout, default=str)
sys.stdout.write("\\n")
"""

    parameters = _exec_return_json(project, script)
    if parameters is None:
        raise HTTPException(404, f"Project '{project}' has no config/parameters.toml")
    return _expect_dict(parameters, project)


@app.put("/catalog/projects/{project}/catalog")
def inject_catalog(project: str, file: UploadFile) -> dict:
    """
    Replace a project's catalog.toml
    """

    return _inject_toml(project, file, "catalog.toml")


@app.put("/catalog/projects/{project}/parameters")
def inject_parameters(project: str, file: UploadFile) -> dict:
    """
    Replace a project's parameters.toml
    """

    return _inject_toml(project, file, "parameters.toml")


@app.put("/catalog/projects/{project}/credentials")
def inject_credentials(project: str, file: UploadFile) -> dict:
    """
    Replace a project's credentials.toml
    """

    return _inject_toml(project, file, "credentials.toml")

# Run --------------------------------------------------------------------------
def _public_run(record: dict) -> dict:
    """
    The fields a client sees for one run
    """

    key = "pipeline" if record["kind"] == "pipelines" else "system"
    return {"project": record["project"], key: record[key], "run": record["run"], "status": record["status"]}


def _run_dir(kind: str, project: str, name: str, run: int) -> Path:
    """
    Directory of one run's log and status. Refuses a name that would leave .logs.
    """

    path = (LOGS_DIR / project / kind / name / str(run)).resolve()
    if not path.is_relative_to(LOGS_DIR.resolve()):
        raise HTTPException(404, f"'{name}' not found")
    return path


def init_runs() -> None:
    """
    Create .logs and load runs recorded there. A run still marked running belonged to a
    previous process, which is gone, so it is recorded as errored.
    """

    LOGS_DIR.mkdir(exist_ok=True)
    loaded = []
    for status_path in LOGS_DIR.glob("*/*/*/*/status"):
        project, kind, name, run_s, _ = status_path.relative_to(LOGS_DIR).parts
        if kind not in ("pipelines", "systems"):
            continue
        try:
            run = int(run_s)
        except ValueError:
            continue
        status = status_path.read_text(encoding="utf-8").strip()
        if status == "running":
            status = "errored"
            status_path.write_text("errored\n", encoding="utf-8")
            with (status_path.parent / "log").open("a", encoding="utf-8") as f:
                f.write("ValveStation restarted while this run was still going\n")
        elif status not in ("finished", "errored"):
            status = "errored"
            status_path.write_text("errored\n", encoding="utf-8")
        key = "pipeline" if kind == "pipelines" else "system"
        loaded.append({"kind": kind, "project": project, key: name, "run": run, "status": status})
    loaded.sort(key=lambda record: (record["project"], record["kind"], record["run"]))
    with runs_lock:
        runs.clear()
        runs.extend(loaded)


def _watch_run(record: dict, proc: subprocess.Popen[bytes], log_file: IO[str]) -> None:
    """
    Wait for a run to exit and record whether it finished or errored
    """

    try:
        status = "finished" if proc.wait() == 0 else "errored"
    except Exception:
        status = "errored"
    finally:
        log_file.close()
    with runs_lock:
        record["status"] = status
    key = "pipeline" if record["kind"] == "pipelines" else "system"
    status_path = _run_dir(record["kind"], record["project"], record[key], record["run"]) / "status"
    status_path.write_text(status + "\n", encoding="utf-8")


def _start_run(kind: str, project: str, name: str) -> dict:
    """
    Start a Canonada pipeline or system. stdout and stderr share one log file, and each
    start gets the next run number for that name.
    """

    key = "pipeline" if kind == "pipelines" else "system"
    with runs_lock:
        taken = [r["run"] for r in runs if r["kind"] == kind and r["project"] == project and r[key] == name]
        run = max(taken, default=0) + 1
        run_dir = _run_dir(kind, project, name, run)
        run_dir.mkdir(parents=True)
        (run_dir / "status").write_text("running\n", encoding="utf-8")
        log_file = (run_dir / "log").open("w", encoding="utf-8")
        record = {"kind": kind, "project": project, key: name, "run": run, "status": "running"}
        runs.append(record)
        try:
            proc = subprocess.Popen(
                [sys.executable, "-P", "-m", "canonada.cli", "run", kind, name],
                cwd=PROJECTS_DIR / project,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        except OSError as e:
            log_file.close()
            record["status"] = "errored"
            (run_dir / "status").write_text("errored\n", encoding="utf-8")
            raise HTTPException(500, f"Could not start Canonada: {e}")
    threading.Thread(target=_watch_run, args=(record, proc, log_file), daemon=True).start()
    return _public_run(record)


@app.post("/run/projects/{project}/pipelines/{pipeline}")
def run_pipeline(project: str, pipeline: str) -> dict:
    """
    Run a pipeline. Its output is written to projects/.logs and the run is tracked until it exits.
    """

    names = [entry["name"] for entry in get_pipelines(project)]
    if pipeline not in names:
        raise HTTPException(404, f"Pipeline '{pipeline}' not found")
    return _start_run("pipelines", project, pipeline)


@app.post("/run/projects/{project}/systems/{system}")
def run_system(project: str, system: str) -> dict:
    """
    Run a system. Its output is written to projects/.logs and the run is tracked until it exits.
    """

    names = [entry["name"] for entry in get_systems(project)]
    if system not in names:
        raise HTTPException(404, f"System '{system}' not found")
    return _start_run("systems", project, system)


# Logs -------------------------------------------------------------------------
def _matching_runs(kind: str, project: str, name: str) -> list[dict]:
    """
    Runs of one pipeline or system, in run order
    """

    key = "pipeline" if kind == "pipelines" else "system"
    with runs_lock:
        return [r for r in runs if r["kind"] == kind and r["project"] == project and r[key] == name]


def _log_response(record: dict) -> PlainTextResponse:
    """
    The text of a run's log file
    """

    key = "pipeline" if record["kind"] == "pipelines" else "system"
    path = _run_dir(record["kind"], record["project"], record[key], record["run"]) / "log"
    if not path.is_file():
        raise HTTPException(404, f"Run {record['run']} has no log file")
    return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"))


@app.get("/logs/pipelines")
def list_pipeline_runs() -> list[dict]:
    """
    Pipeline runs and whether each is running, finished, or errored
    """

    with runs_lock:
        return [_public_run(r) for r in runs if r["kind"] == "pipelines"]


@app.get("/logs/systems")
def list_system_runs() -> list[dict]:
    """
    System runs and whether each is running, finished, or errored
    """

    with runs_lock:
        return [_public_run(r) for r in runs if r["kind"] == "systems"]


@app.get("/logs/projects/{project}/pipelines/{pipeline}")
def read_pipeline_log(project: str, pipeline: str) -> PlainTextResponse:
    """
    The log of the latest run of a pipeline
    """

    matched = _matching_runs("pipelines", project, pipeline)
    if not matched:
        raise HTTPException(404, f"No runs of pipeline '{pipeline}' in project '{project}'")
    return _log_response(max(matched, key=lambda record: record["run"]))


@app.get("/logs/projects/{project}/pipelines/{pipeline}/{run}")
def read_pipeline_run_log(project: str, pipeline: str, run: int) -> PlainTextResponse:
    """
    The log of one pipeline run
    """

    matched = [r for r in _matching_runs("pipelines", project, pipeline) if r["run"] == run]
    if not matched:
        raise HTTPException(404, f"Run {run} of pipeline '{pipeline}' in project '{project}' not found")
    return _log_response(matched[0])


@app.get("/logs/projects/{project}/systems/{system}")
def read_system_log(project: str, system: str) -> PlainTextResponse:
    """
    The log of the latest run of a system
    """

    matched = _matching_runs("systems", project, system)
    if not matched:
        raise HTTPException(404, f"No runs of system '{system}' in project '{project}'")
    return _log_response(max(matched, key=lambda record: record["run"]))


@app.get("/logs/projects/{project}/systems/{system}/{run}")
def read_system_run_log(project: str, system: str, run: int) -> PlainTextResponse:
    """
    The log of one system run
    """

    matched = [r for r in _matching_runs("systems", project, system) if r["run"] == run]
    if not matched:
        raise HTTPException(404, f"Run {run} of system '{system}' in project '{project}' not found")
    return _log_response(matched[0])

# Misc -------------------------------------------------------------------------
@app.get("/version")
def get_version() -> dict:
    """
    The version of the server
    """
    from valvestation._version import __version__
    return {"version": __version__}

@app.get("/health")
def get_health() -> dict:
    """
    The health of the server
    """
    with runs_lock:
        return {"health": "idle" if all(r["status"] != "running" for r in runs) else "running"}

# MAIN -------------------------------------------------------------------------

def main() -> None:
    """
    Install a station directory, or start the server in the working directory
    """

    installing = len(sys.argv) > 1 and sys.argv[1] == "install"
    logging.basicConfig(
        format="%(asctime)s - %(name)s: [%(levelname)s]: %(message)s",
        level=logging.INFO if installing else logging.WARNING,
    )
    if installing:
        install()
        return

    check_install()  # First thing at boot
    global config
    config = load_config()

    # Leftovers of uploads and removals interrupted by a restart
    for leftover in PROJECTS_DIR.glob(".staging-*"):
        shutil.rmtree(leftover, ignore_errors=True)

    # Build the run list from projects/.logs. It then lives until this process exits.
    init_runs()

    host = os.environ.get("VALVESTATION_HOST", "127.0.0.1")
    port = int(os.environ.get("VALVESTATION_PORT", "508"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
