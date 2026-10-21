# The optional extras (pyproject.toml [project.optional-dependencies]) baked into each Python
# variant of the package. The Nix store is read-only, so nothing can be installed lazily later:
# a variant carries everything it will ever import.
#
# Plain data on purpose: nix/packages.nix and nix/checks.nix import it, and the Python tests read
# it without nixpkgs. Keep the shape `name = [ "..." ];` or `name = other ++ [ "..." ];`.
let
  # Messaging adapters whose clients are not in the base dependencies (Telegram, Matrix without
  # encryption, Signal, email and the webhook platforms need only the base set).
  messaging = [
    "discord"
    "slack"
    "mattermost"
    "google-chat"
    "qqbot"
    "dingtalk"
    "wecom"
    "feishu"
    "simplex"
    "buzz"
    "weixin"
    "yuanbao"
  ];
in
{
  minimal = [ ];

  inherit messaging;

  # Everything portable that a desktop or server user expects to just work. Left out until the Nix
  # lane shows they build: extras whose wheels bundle foreign binaries (browser, voice, research,
  # the cloud sandbox SDKs, discord-voice) and matrix-e2ee (Linux only, python-olm builds from its
  # sdist with cmake). The modules' extraPythonPackages covers them meanwhile.
  default = messaging ++ [
    "web"
    "docs"
    "pty"
    "dashboard"
    "acp"
  ];
}
