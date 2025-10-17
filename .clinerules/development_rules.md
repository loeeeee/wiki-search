# Development Guideline

Keep project simple.

## Environment Management

Expect python 3.12 and above.

The development environment is NixOS.

Always use virtual environment at .venv/

Always use shell.nix to config project environment.

Always use uv for python environment management.

## Coding

Always Python logging system, and default log output to console, in addition to files.

Always use Python typing system.

Always test run the code after each task is finished.

Never use emoji in the code and documentation.

When needed, prefer using dataclass the normal python objects.

Always add tqdm progress bar to the process that would take a long time. (>10s)

Always follow the existing code structure.

Always import python module on top of the file.

Always exam the existing code logic first with a global view of the code, and state how the implementation would change the code logic.

## Documentation

Always document concise implementation details and basic usage in docs-vibe/ with a sequential file name.

Always update README.md after each task.
