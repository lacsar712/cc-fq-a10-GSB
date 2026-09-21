"""Shared pytest fixtures: isolate tests on a throwaway SQLite database.

DATABASE_URL must be set before any `app.*` import, because app.database
creates the engine at import time.
"""

import os
import tempfile

_DB_PATH = os.path.join(tempfile.gettempdir(), "fastq_qc_pytest.db")
if os.path.exists(_DB_PATH):
    os.remove(_DB_PATH)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ.setdefault("JWT_SECRET", "fastq-qc-pipeline-dev-secret")
