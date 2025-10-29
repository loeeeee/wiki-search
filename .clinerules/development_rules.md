# Development Guideline

Keep project simple.

## Environment Management

Expect python 3.12 and above.

The development environment is NixOS.

Always use virtual environment at .venv/

Always use shell.nix to config project environment.

Always use uv for python environment management.

Always run python using nix-shell.

Always update both uv and nix.shell to include new dependency.

## Coding

Always Python logging system, and default log output to console, in addition to files.

Always use Python typing system.

Always test run the code after each task is finished.

Never use emoji in the code and documentation.

Always define data structure with dataclass.

When data type is too simple to be defined as dataclass, always create a NamedDict for dictionary.

Always add tqdm progress bar to the process that would take a long time. (>10s)

Always follow the existing code structure.

Always import python module on top of the file.

Always exam the existing code logic first with a global view of the code, and state how the implementation would change the code logic.

When optimizing CPU utilization, always use concurrent.futures instead of multiprocessing, unless the user explicit agree the usage of multiprocessing.

Never use module level constant.

Always import module at the top of the file, unless specifically consent by the user.

Always use fail-fast design.

Always check and remove dead code in the script.

Always use a modular design with helper functions.

Always print a concise report after script execution.

## Documentation

Always document concise implementation details and basic usage in docs-vibe/ with a sequential file name.

When writing documentation in docs-vibe/, always document the user's intent in its original words, in addition to a more logical and concise rephrasing.

Always create documentation in docs-vibe/ before start writing any code.

Always update the newly created documentation at the end of the task to reflect the latest status of the code.

Always update README.md after each task.

If the task could not be finished in one-shot, always create a reflection documentation in docs-vibe/reflection after each task. The reflection document needs to describe the circumenstance that is causing the issues, and the solution, in addition to other important information. When a reflection document is created, never repeat its content in development report in docs-vibe.
