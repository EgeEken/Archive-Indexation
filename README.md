# Archive-Indexation

A local-first tool for indexing, selecting, compressing, and browsing personal media archives.

## Development setup

The production application lives under `src/archive_index/`. `Early Testing/` is a frozen research record and is not required for normal startup.

Install `uv`, then let it create and manage the project environment from `pyproject.toml` and `uv.lock`:

```powershell
uv sync
```

Run the application shell or its health check:

```powershell
uv run archive-index run
uv run archive-index doctor
```

Start the localhost UI at the workspace home:

```powershell
uv run archive-index serve
```

The home screen remembers opened workspaces in the ignored repository-root file
`.archive-index-workspaces.json`. This is convenience state only; each workspace's
canonical database remains under its own `.archive-index/` directory. A workspace
path can be entered manually or selected with the native folder picker. Opening or
creating a workspace starts incremental indexing automatically.

Start the localhost UI directly for an existing workspace:

```powershell
uv run archive-index serve C:\path\to\workspace
```

Create/open a workspace, scan it, and process metadata and thumbnails with concise progress output:

```powershell
uv run archive-index index C:\path\to\workspace
```

The server binds to `127.0.0.1` by default. Open the printed URL in a browser. The UI supports gallery pagination, folder/type/filename filters, square photo cards, image/video viewing, asset details, thumbnail/original serving, indexing progress, and Problems.

Run the standard-library test suite:

```powershell
uv run python -m unittest discover -s tests -v
```
