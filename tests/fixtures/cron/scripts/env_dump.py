# Environment fixture (P14-09): print the names and values the script was given.
import os

for name in sorted(os.environ):
    print(f"{name}={os.environ[name]}")
