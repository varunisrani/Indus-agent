# Saved logins (vault)

The browser tools can sign in, pay and fill addresses for you without the agent ever seeing the
password, card number or address. The agent works with handles; the values are looked up on your
machine and typed straight into the page.

## Where the values come from

`browser.vault_sources` lists the sources the agent may use (all three by default):

| Source | What it needs |
|---|---|
| `local` | the local vault, managed with `indus vault` (needs the `vault` [extra](../getting-started/install.md#what-gets-installed)) |
| `onepassword` | the 1Password command-line tool (`op`, version 2) installed |
| `bitwarden` | the Bitwarden command-line tool (`bw`) installed |

A command-line source is used only when its program is installed. When a password manager is
locked, the agent can ask you to unlock it: you type the master password into a hidden prompt, and
it is handed to the manager's program, never to the model.

## The local vault

```bash
indus vault add --kind login --origin https://shop.example.com --label "Shop account"
indus vault list
indus vault remove <handle>
```

`add` saves a login (the site, a label, the user name, then the password and an optional
authenticator key), a payment card or an address. Values are only ever typed into hidden prompts,
never given as arguments, so `add` needs a terminal. `list` shows handles and labels, never values.

The vault is one encrypted file, `<home>/vault/local.vault`, readable only by you; its key is kept
in the profile's secret store (the system keychain when it works). `indus vault lock` stops the
agent from using it until `indus vault unlock`, or until you approve an unlock request in a chat.

## How filling stays safe

- An item is filled only on the exact site (origin) it is saved for. The page's address is checked
  before filling and again at the moment of filling, so a page that redirected in between gets
  nothing.
- Card details are filled only after you approve a request in the chat.
- The agent can ask to save the login of the current page: you type the user name and password
  yourself, and they are stored in the local vault for that site.
- One-time codes come from the item's authenticator key when it has one, otherwise you are asked.
- Every value that was filled is masked if it later shows up in a page snapshot, and tool results
  carry only handles, counts, sites and outcomes.
