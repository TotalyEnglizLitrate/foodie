# foodie

A unified Swiggy/Zomato TUI. Search both providers side by side, build a cart,
pay with a UPI QR or cash on delivery, and track your orders.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). Credentials are kept
in your system keyring, so a keyring backend (e.g. the D-Bus secret service on
Linux) must be available.

To run from a checkout:

```sh
uv sync
uv run foodie
```

To install `foodie`
```sh
uv tool install git+https://github.com/TotalyEnglizLitrate/foodie   # or: pipx install git+https://github.com/TotalyEnglizLitrate/foodie
foodie
```

A Nix flake is also provided if you prefer it (`nix develop/run github:TotalyEnglizLitrate/foodie`).

## Usage

```
foodie [-v] [COMMAND]
```

With no command, `foodie` launches the TUI.

### Logging in

Log in from the TUI with the provider buttons, or from the command line:

```sh
foodie auth login [-p swiggy|zomato] [--port 8080] [--force]
foodie auth logout [-p swiggy|zomato]
foodie auth status [-p swiggy|zomato]
```

- `login` opens the provider's OAuth page in your browser (phone + OTP) and
  receives the callback on `localhost:<port>`. Providers that are already
  logged in are skipped unless you pass `--force`.
- `-p/--provider` can be repeated; leave it out to act on both providers.
- `status` exits with code 1 if any selected provider is logged out.

## Logging

Logs are written at INFO by default. Pass `-v/--verbose` (before any
subcommand, e.g. `foodie -v auth login`) for DEBUG.

| Where | When |
| --- | --- |
| `~/.local/state/foodie/foodie.log` (or `$XDG_STATE_HOME/foodie/foodie.log`) | always |
| stderr | commands other than the TUI, which owns the terminal |

At DEBUG the log includes every HTTP request and response to the providers
(bodies truncated to 2000 characters), tool calls with their arguments and
timings, token refreshes, and keyring reads and writes. Access and refresh
tokens, authorization codes, PKCE verifiers, client secrets and `Bearer`
headers are masked as `***`, but the logs still contain your addresses, orders
and menu data, so review them before sharing.

Unexpected errors in the TUI are logged with a traceback at WARNING.
