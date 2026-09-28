import tomllib
from fastapi import FastAPI, File, UploadFile

app = FastAPI()

# Project
@app.put("/project/add")
def add_project()


# Registry
# @app.get("/registry/pipelines")
# def read_registry_pipelines():
#     import canonada as c
#     c.pipeline.

# @app.get("/items/{item_id}")
# def read_item(item_id: int, q: str | None = None):
#     return {"item_id": item_id, "q": q}
