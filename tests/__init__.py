import os
import tempfile

# Snapshot stores go to $XDG_DATA_HOME/cracked/snapshots: keep the tests' out of the real one.
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="cracked-test-data-")
