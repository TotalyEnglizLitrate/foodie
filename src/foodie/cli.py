from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Annotated

import typer

from foodie.auth import authenticate_interactive
from foodie.log import log_path, setup_logging
from foodie.providers.base import BaseProvider
from foodie.providers.swiggy import SwiggyProvider
from foodie.providers.zomato import ZomatoProvider


class ProviderChoice(StrEnum):
    swiggy = "swiggy"
    zomato = "zomato"


app = typer.Typer(help="A unified Swiggy/Zomato TUI.", no_args_is_help=False)
auth_app = typer.Typer(help="Manage provider logins.", no_args_is_help=True)
app.add_typer(auth_app, name="auth")

ProviderArg = Annotated[
    list[ProviderChoice] | None,
    typer.Option(
        "--provider",
        "-p",
        help="Provider to act on; repeat for several (default: all).",
    ),
]


def _providers(choices: list[ProviderChoice] | None) -> list[BaseProvider]:
    wanted = set(choices or ProviderChoice)
    return [
        provider
        for choice, provider in (
            (ProviderChoice.swiggy, SwiggyProvider()),
            (ProviderChoice.zomato, ZomatoProvider()),
        )
        if choice in wanted
    ]


@app.callback(invoke_without_command=True)
def launch(
    ctx: typer.Context,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help=f"Log at DEBUG instead of INFO. Logs go to {log_path()}.",
        ),
    ] = False,
) -> None:
    """Launch the TUI when no subcommand is given."""
    # The TUI owns the terminal, so it only logs to the file.
    setup_logging(verbose, console=ctx.invoked_subcommand is not None)
    if ctx.invoked_subcommand is None:
        from foodie.tui import main

        main()


@auth_app.command()
def login(
    provider: ProviderArg = None,
    port: Annotated[int, typer.Option(help="Local port for the OAuth callback.")] = 8080,
    force: Annotated[
        bool, typer.Option("--force", "-f", help="Log in again even if already logged in.")
    ] = False,
) -> None:
    """Log in to a provider through the browser."""

    async def run() -> bool:
        ok = True
        for p in _providers(provider):
            if await p.is_authenticated():
                if not force:
                    typer.echo(f"{p.display_name}: already logged in (use --force to redo).")
                    continue
                await p.logout()
            ok = await authenticate_interactive(p, port=port) and ok
        return ok

    if not asyncio.run(run()):
        raise typer.Exit(1)


@auth_app.command()
def logout(provider: ProviderArg = None) -> None:
    """Remove stored credentials."""

    async def run() -> None:
        for p in _providers(provider):
            await p.logout()
            typer.echo(f"{p.display_name}: logged out.")

    asyncio.run(run())


@auth_app.command()
def status(provider: ProviderArg = None) -> None:
    """Show whether each provider is logged in."""

    async def run() -> bool:
        all_ok = True
        for p in _providers(provider):
            authed = await p.is_authenticated()
            all_ok = all_ok and authed
            typer.echo(f"{p.display_name}: {'logged in' if authed else 'not logged in'}")
        return all_ok

    if not asyncio.run(run()):
        raise typer.Exit(1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
