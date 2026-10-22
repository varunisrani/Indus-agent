# Pets

A pet is a small companion figure you design yourself. It is drawn by the image service set up in
your profile (`image_gen.*`), shown on the desktop as a small figure that wanders over your work
area, and as a card in the terminal UI. Nothing is downloaded: every pet is drawn for you.

## Making a pet

```bash
indus pet generate "a small red fox with a blue scarf"
```

This draws up to four looks (`--count 1` to `4`) and prints a token and the numbered drafts. Drafts
are kept for an hour. Pick one and hatch it:

```bash
indus pet hatch <token> --pick 2 --name Pip --use
```

Hatching draws the pet once for each of its six moods (resting, walking, talking, thinking, sleeping,
cheering), using the draft as the reference, and saves the pictures under `<home>/pets/<slug>/`.
Generating may take up to 7 minutes and hatching up to an hour, depending on the image service.
Without `--use` the new pet is not shown yet.

## Showing and managing pets

| Command | What it does |
|---|---|
| `indus pet list` | the pets of this profile; the shown one is marked |
| `indus pet show [SLUG]` | one pet's idea, scale, folder and pictures |
| `indus pet use SLUG` | show this pet on the desktop and in the TUI |
| `indus pet off` | hide the pet everywhere |
| `indus pet rename SLUG NEW NAME` | give it a new name |
| `indus pet remove SLUG [-y]` | delete a pet and its pictures |

In the TUI, `/pet` shows the shown pet's card; `/pet list`, `/pet use <slug>` and `/pet off` work
there too.

On the desktop the pet lives in its own see-through window: clicks pass through everywhere except
the figure itself, it says short status lines in a bubble, and it hides while a full-screen window
is in front.

| Setting | Default | Meaning |
|---|---|---|
| `pet.enabled` | `false` | show the pet (`use` turns it on, `off` turns it off) |
| `pet.active` | empty | the slug of the pet to show |
| `pet.scale` | `1.0` | size on every surface, from 0.25 to 3 |
