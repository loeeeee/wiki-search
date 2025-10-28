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

When optimizing CPU utilization, always use concurrent.futures instead of multiprocessing, unless the user explicit agree the usage of multiprocessing.

Always avoid module level constant.

Always import module at the top of the file, unless specifically consent by the user.

For computational tasks, never fallback to slower procedure, always raise errors when command cannot be completed.

### Standard Process Management

- In a Django Project, when efficienct computation is needed on CPU, the script should always employ a producer and consumer model. The ratio of which should be determined by the nature of the task. 
- In the main process, a script should spawn the same number of thread as the number of CPU threads available to retrieve information from database as producer.
- The script should by default spawn the same number of consumer processes as the number of CPU cores.
- The consumer process should never need Django context to start. They should take lean data type, and return lean data type.
- When converting data from and to Django objects overwhelms the main process, the script should skip the Django ORM and directly interact with database.

## Documentation

Always document concise implementation details and basic usage in docs-vibe/ with a sequential file name.

When writing documentation in docs-vibe/, always document the user's intent in its original words, in addition to a more logical and concise rephrasing.

Always create documentation in docs-vibe/ before start writing any code.

Always update the newly created documentation at the end of the task to reflect the latest status of the code.

Always update README.md after each task.
