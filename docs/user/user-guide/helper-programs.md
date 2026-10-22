# Helper programs (`indus pm`)

Some tools use programs outside Python: ripgrep and uv (required), Node, ffmpeg and Chromium
(installed by default), and gh, whisper.cpp and llama.cpp (optional). `indus pm` installs them into
Indus's own folder, `<root>/pm/`, checks each download against a pinned SHA-256, and never touches
your system's copies.

```bash
indus pm lock --yes        # pin a download URL and SHA-256 for every package (first time)
indus pm install           # the required packages plus the default ones
indus pm doctor            # check what is installed
```

## Pins

`indus pm lock` writes `<root>/pm/lock.json`: for each package a version and, for every kind of
machine, a URL and its SHA-256. It downloads every file to hash it, so without a terminal it needs
`--yes`. `install` refuses until something is locked.

`indus pm update` moves pins to the newest release that is at least 14 days old and installs what
changed; `--check` only reports (exit 1 when updates exist), `--yes` applies without asking.

## Installing

| Command | What it does |
|---|---|
| `indus pm install [NAMES]` | install the named packages, or the required plus default ones |
| `indus pm install --without ffmpeg` | leave a default package out, now and later (name it in `install` again to opt back in) |
| `indus pm env [NAMES]` | print, as JSON, the folders installed packages add to `PATH` |
| `indus pm doctor` | compare what is installed with the pins: ok, not installed, outdated, missing program, or changed on disk |
| `indus pm repair` | rebuild damaged packages at their recorded versions (the pins are not changed) |
| `indus pm gc` | remove downloads and installed folders nothing refers to any more |
| `indus pm status [--json]` | what the last `pm` command did, step by step |

A default package that fails to install is a warning, not a failure. `doctor` exits 1 when a
required package is not usable or any package was changed on disk.

## Offline machines

```bash
indus pm bundle --out /media/usb/indus-pm          # on a connected machine
indus pm install --from /media/usb/indus-pm        # on the offline one
```

`bundle` copies the pins, the install records and the downloaded files into an empty folder;
`install --from` installs from it without the network.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `pm.mirror_url` | empty | a mirror tried after a file's own URL (`<mirror_url>/sha256/<hash>`); every file must still match its pin |
| `pm.timeout_s` | `60` | seconds one network request may take |

Interrupted downloads resume where they stopped.
