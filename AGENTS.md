# Chemotion Sample Manager

- Keep application code in `src/chemotion_sample_manager/` and Python dependencies in `pyproject.toml`.
- Source launchers in `launchers/macos/` and `launchers/windows/` use `bootstrap.py` to manage a private virtual environment automatically.
- Preserve streamed inventory rows and structure thumbnails when loading is stopped.
- Validate launcher changes without opening the GUI. Use targeted tests and Python compilation checks.
