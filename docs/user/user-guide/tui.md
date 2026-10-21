# Terminal UI

The terminal UI is a full-screen interface for the same agent: a scrolling transcript with
collapsible tool and reasoning sections, a multi-line composer, cards for approvals and questions,
and a status rule with the model, the context in use and the session state.

```bash
indus --tui                                # this run only
indus config set display.interface tui     # make it the default; --cli goes back
```

Like the classic CLI it can host the agent in its own
process (`--local`) or attach to this profile's gateway daemon, so the same conversation can be
followed from another terminal, the desktop app or the dashboard.

## Keys

| Key | What it does |
|---|---|
| Enter | send (or apply the highlighted completion) |
| Shift+Enter, Alt+Enter | new line |
| Ctrl+C | clear the draft; with an empty draft stop the reply; when idle and empty, leave |
| Esc Esc | discard the draft, or stop the running reply |
| Ctrl+D | leave (only with an empty draft) |
| Up, Down | completion list, then queued messages, then your earlier messages |
| PgUp, PgDn, Shift+Up, Shift+Down | scroll the transcript |
| Ctrl+O | model picker |
| Ctrl+X | session switcher (with a selection: cut) |
| Shift+Tab | turn auto-approval (yolo) on or off for this session |
| Ctrl+G | edit the draft in `$EDITOR` |
| Ctrl+K | send the next queued message now |
| Ctrl+Z, Ctrl+Y | undo, redo in the composer |
| Ctrl+L | redraw the screen |

On macOS, Cmd works wherever Ctrl is listed, in terminals that report it.

While a reply runs you can keep typing. What Enter does with the text then depends on the busy mode:
by default it is queued and sent when the reply ends (Up walks the queue so you can edit an item);
`/steer <text>` adds guidance to the running reply instead. Two quick presses of Enter on an empty
composer stop the reply.

## Cards

Approvals, questions from the agent (`clarify`), sudo passwords and secrets the agent asks for
appear as cards above the composer. Pick an answer with the arrow keys and Enter or with its
number. Ctrl+C on a card answers it the safe way (deny, or no answer). Masked inputs never echo
what you type and never reach the transcript.

## Settings

| Key | Choices |
|---|---|
| `tui.theme` | `auto` (detects a light or dark terminal) or a theme name |
| `tui.details` | tool and reasoning sections start `collapsed` or `expanded` |
| `tui.tool_progress` | how much tool activity shows: `off`, `new`, `all`, `verbose` |
| `tui.indicator` | `spinner`, `dots` or `none` while a reply runs |
| `tui.statusbar` | show the status rule |
| `tui.mouse` | let the UI capture the mouse (wheel scrolling) |

The [slash commands](../reference/slash-commands.md) work here as in the classic CLI; `/help` lists
them.
