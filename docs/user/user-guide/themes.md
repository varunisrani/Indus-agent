# Themes

## Desktop app

Open **Settings → Appearance → Theme**.

- **Colour theme** picks light, dark, or following the system.
- **Colours** picks one of twelve built-in themes (Indus, Monsoon, Laterite, Saffron, Indigo,
  Basalt, Lotus, Mangrove, Dune, Harbour, Terrace, Jade). Each has a light and a dark side, and
  every one keeps text readable (WCAG AA contrast) in both.
- **Accent hue** changes the accent colour; the app adjusts it as needed so text on it stays
  readable. **Use the theme's accent** undoes it.
- **For this profile only** gives the foreground profile its own theme, chat font and terminal font,
  so you can tell profiles apart at a glance.

### VS Code themes

**Import a VS Code theme** searches the VS Code Marketplace (an empty search shows the most
installed), downloads the extension you pick, converts its colours to an Indus theme and installs
it as one of yours, marked *(yours)* in the list. Installing one you already have switches back to
it; **Remove** deletes it. Imported themes are kept per device. The app talks only to the
Marketplace's own hosts for this.

## Terminal UI

The TUI reads `tui.theme`: `auto` (the default) detects a light or dark terminal, or set a theme
name. In the desktop app, **Use this light or dark choice in the terminal UI too** writes the
desktop's current light or dark choice to `tui.theme` for the foreground profile.

## Web dashboard

`dashboard.theme` is `system`, `light` or `dark`; see [Web dashboard](dashboard.md).

## Languages

Indus Agent ships in English only for now.
