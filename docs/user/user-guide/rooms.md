# Group rooms

A group room is a lasting chat in which several of your [bots](bots-and-peers.md) (profiles) talk
with you and with each other. The gateway keeps the room's history, decides whose turn it is and
runs each member's turn, so a room keeps going while no app is open.

## Where rooms live

Rooms belong to the Indus root, not to one profile: every room is kept in `<root>/rooms.db`. The
gateway daemon drives them, so the gateway must be running (`indus gateway start`). A member's turn
runs in that member's own profile, which this gateway must serve: when the members are different
profiles, turn on `gateway.multiplex_profiles` so one gateway serves every profile under the root.
A member whose profile is not served fails its turn with a message saying so.

In the desktop app, the **Bots** page lists the rooms under *Group rooms*. Open one to read it,
write to it (**Send**), stop the turns in progress (**Stop**), retry a turn that needs a decision,
or **Disband** it. Programs reach the same actions through the `groups.*` methods of the
[protocol](../reference/protocol.md); a room is created with `groups.create`, naming its members by
profile.

## Whose turn it is

- Your message gives a turn to each member it @mentions (`@all` or `@everyone` names every member),
  or to the first member when it mentions nobody.
- When every member that was asked has answered, the member after the last speaker (in room order)
  goes next, so members can answer each other.
- One message of yours leads to at most 8 member turns. A member that says nothing, a failed turn,
  a stop, or a new message from you ends the chain.
- Members take turns one at a time. A member that stops reporting progress for 3 minutes loses its
  turn; no turn runs longer than 3 hours.

## Stopping

**Stop** cancels every queued turn and asks the running one to stop. In a message, a stop word
(`stop`, `halt` or `pause`) right next to a member's @mention holds that member: `stop @writer`,
`@writer stop`, `@writer please pause`. `@all stop` holds everyone. A held member takes no turn
until a later message of yours @mentions it again. The same words elsewhere in a message, or inside
code, quotes or block quotes, are ordinary text.

## Turns that need a decision

When you stop the gateway, the turns in progress end as failed. If the gateway dies in the middle
of a turn instead (a crash, a killed process), nobody knows how far the turn got, so it is never
started again on its own: running it twice could repeat what it did. The room shows it as waiting
for you; **Retry turn** runs it again once you have decided that is safe.

## Disbanding

Disbanding ends a room for every member. Its history stays readable, and its id is never used
again: a new room with the same name is a fresh room. Disbanded rooms are removed after 90 days.

## Limits

A room holds at most 128 members and 50,000 events (256 MiB); at most 256 rooms are active at
once. A full room still accepts a stop or a disband.
