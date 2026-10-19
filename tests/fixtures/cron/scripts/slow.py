# Slow fixture (P14-09): the leader starts a grandchild, records both pids in the working folder,
# then both block on a pipe nobody writes to (no sleeping), until they are killed.
import os
import subprocess
import sys

block = "import os\nr, w = os.pipe()\nos.read(r, 1)\n"
child = subprocess.Popen([sys.executable, "-c", block], stdin=subprocess.DEVNULL)
with open("pids.txt", "w", encoding="utf-8") as fh:
    fh.write(f"{os.getpid()} {child.pid}\n")
print("started", flush=True)
r, w = os.pipe()
os.read(r, 1)
