# Contributors

Release notes credit the people whose commits a release contains. This folder maps a commit
author to the name the notes print.

- One file per author email: `<key>.txt`, where `<key>` is the first 16 hex digits of the SHA-256
  of the lower-cased email address. The file's first non-empty line is the name to credit (a
  GitHub handle such as `@someone`, or a full name). No email address is ever written into this
  repository: not in a file name, not in a file.
- A stable release refuses to cut while any commit since the previous release has an author with
  no file here; the refusal names each missing `<key>.txt` (a canary only warns).
- Compute a key (with the address in `AUTHOR_EMAIL`) with:

  ```sh
  printf '%s' "$AUTHOR_EMAIL" | tr 'A-Z' 'a-z' | sha256sum | cut -c1-16
  ```

The map starts empty on purpose: it is personal data, and nothing is copied in from elsewhere.
