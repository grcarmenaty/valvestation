"""
ValveStation: an HTTP API to control the Canonada projects

Configuration is read from config.toml in the working directory. Run with:
    python src/main.py
"""

import hmac
import json
import logging
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import tomllib
import zipfile
import zlib
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

CONFIG_PATH = Path("config.toml").resolve()
PROJECTS_DIR = CONFIG_PATH.parent / "projects"  # One sub-directory per Canonada project

log = logging.getLogger("valvestation")

projects_lock = threading.Lock()  # Serialises adding and removing projects

app = FastAPI()


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
async def check_token(request: Request, call_next):
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
def add_project(file: UploadFile):
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
def remove_project(project: str):
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


@app.get("/registry/projects")
def get_projects():
    """
    List available projects
    """
    return {"projects": [p.name for p in PROJECTS_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")]}


@app.get("/registry/projects/{project}/pipelines")
def get_pipelines(project: str):
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

    return _exec_return_json(project, script)


@app.get("/registry/projects/{project}/systems")
def get_systems(project: str):
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

    return _exec_return_json(project, script)


# View -------------------------------------------------------------------------
@app.get("/view/projects/{project}/pipelines/{pipeline}")
def view_pipeline(project: str, pipeline: str):
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
    return view


@app.get("/view/projects/{project}/systems/{system}")
def view_system(project: str, system: str):
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
    return view

# MAIN -------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s - %(name)s: [%(levelname)s]: %(message)s")
    check_install()  # First thing at boot
    config = load_config()

    # Leftovers of uploads and removals interrupted by a restart
    for leftover in PROJECTS_DIR.glob(".staging-*"):
        shutil.rmtree(leftover, ignore_errors=True)

    uvicorn.run(app)
