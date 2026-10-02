# ValveStation

## Install

From a checkout:

```bash
pip install .
```

In the directory that will hold the station, copy the packaged config and create `projects/`:

```bash
valvestation install
```

Set `token` in `config.toml`. `canonada_timeout` is how many seconds Canonada may take to load a project. An existing `config.toml` is left unchanged.

Start the server in that same directory:

```bash
valvestation
```

It listens on `127.0.0.1:508`. `VALVESTATION_HOST` and `VALVESTATION_PORT` override the address.

## Docker

Build the image, then install a station into a mounted directory and run it:

```bash
docker build -t valvestation .
docker run --rm -v "$PWD:/station" valvestation install
docker run --rm -p 508:508 -v "$PWD:/station" valvestation
```

The image sets `VALVESTATION_HOST=0.0.0.0`. Set `token` in the mounted `config.toml` before the second command. `projects/` in that directory is where uploaded Canonada projects are stored, and `projects/.logs` holds run logs.

## API Endpoints

To get an API response the client will need to send a pre-shared token, that will be user set using a configuration file. The valve station config will accept the token as a connection string.

### Catalog
- GET <prefix>/view/catalog: View catalog entries available in the project
- GET <prefix>/view/parameters: View parameters available in the project
- PUT <prefix>/inject/parameters: Inject a new parameters file, specify a project (project aware)
- PUT <prefix>/inject/catalog: Inject a new catalog file, specify a project (project aware)
- PUT <prefix>/inject/credentials: Inject a new credentials file, specify a project (project aware)

### Registry
- GET <prefix>/pipelines: List available pipelines and their descriptions (per project)
- GET <prefix>/systems: List available systems and their descriptions (per project)
- GET <prefix>/projects: Lists the available Canonada projects in this instance

### View
- GET <prefix>/pipeline/{project}/{pipeline}: View a pipeline's internal makeup (nodes and IO)
- GET <prefix>/system/{project}/{system}: View a system internal makeup (list of sequential pipeline)

### Run
- POST <prefix>/pipeline: Run a pipeline and save its full output into a log file -> API keeps track of the running process state (internal list)
- POST <prefix>/system: Run a system and save its full output into a log file -> API keeps track of the running process state

### Logs
- GET <prefix>/pipelines: Read/List pipeline execution status (running/errored/finished)
- GET <prefix>/systems: Read/List system execution status (running/errored/finished)
- GET <prefix>/pipeline/{project}/{pipeline}: Read pipeline logs. Read from the log file for the requested process
- GET <prefix>/pipeline/{project}/{system}: Read system logs. Read from the log file for the requested process

### Project
- PUT <prefix>/add: Receive a Canonada project and index its catalog files and pipelines. -> If the project name is the same the project gets ovewritten
- DELETE <prefix>/remove/{project}: Remove a Canonada project from the station. (By project name)

### Misc
- GET /version
- GET /health
