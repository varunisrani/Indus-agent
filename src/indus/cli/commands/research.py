"""``indus research``: turn the agent into a data engine (P22-09).

Verbs: ``batch DATASET --run-name NAME …`` runs a JSONL of prompts on a process pool and writes
training samples (``indus.research.batch``); ``distributions [NAME]`` lists the toolset
distributions, most likely entries first. ``compress``, ``swe`` and ``sample`` are listed so the
family is visible, and say plainly that they come with a later build (exit 1).

``batch`` exit codes: 0 done, 1 runtime failure (an unreadable prefill file included), 2 usage or
dataset error (a checkpoint for another dataset included), 130 interrupted.

Reasoning: ``--no-reasoning`` wins and sends effort ``none``; else ``--reasoning-effort`` (one of
``REASONING_CHOICES``); neither sends nothing, and the effective config says "provider default"
(R10.4). Provider routing lists are comma strings, split and trimmed (empty items dropped). Keys
never go on the command line: the run uses the profile's credentials.

Everything heavy is imported inside the verbs, so ``indus research --help`` stays light.
``indus.research`` sits one layer below the user surfaces in the layer contract, so the verbs
import it directly.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import click

from indus.cli.cmdkit import cli_state, err, group

if TYPE_CHECKING:
    from indus.research.batch import BatchSettings

__all__ = ["REASONING_CHOICES", "SORT_CHOICES", "research_group", "split_csv"]

REASONING_CHOICES = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
SORT_CHOICES = ("price", "throughput", "latency")
EXIT_FAILED = 1
EXIT_USAGE = 2
LATER = "comes with a later build of the research tooling (P22-{task}); it is not available yet"


def split_csv(value: str | None) -> list[str] | None:
    """``"a, b,,c "`` → ``["a", "b", "c"]``; nothing left → ``None``."""
    if value is None:
        return None
    items = [part.strip() for part in value.split(",")]
    kept = [item for item in items if item]
    return kept or None


def _exit(code: int) -> click.exceptions.Exit:
    return click.exceptions.Exit(code)


def _prefill(path: str | None) -> tuple[Mapping[str, Any], ...]:
    if path is None:
        return ()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        err(f"research batch: cannot read the prefill file {path}: {exc}")
        raise _exit(EXIT_FAILED) from None
    if not isinstance(data, list):
        err(f"research batch: the prefill file {path} must hold a JSON array of messages")
        raise _exit(EXIT_FAILED)
    items = cast(list[Any], data)
    if not all(isinstance(m, dict) and isinstance(m.get("role"), str) for m in items):
        err(f"research batch: every prefill message in {path} needs a 'role'")
        raise _exit(EXIT_FAILED)
    return tuple(cast(Mapping[str, Any], m) for m in items)


def _routing(
    allowed: str | None, ignored: str | None, order: str | None, sort: str | None
) -> dict[str, Any] | None:
    routing: dict[str, Any] = {}
    for key, value in (("only", allowed), ("ignore", ignored), ("order", order)):
        items = split_csv(value)
        if items is not None:
            routing[key] = items
    if sort:
        routing["sort"] = sort
    return routing or None


@group("research", "Batch runs, toolset distributions and trajectory tools.")
def research_group() -> None:
    pass


@research_group.command("batch")
@click.argument("dataset", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--run-name", required=True, help="Name of the run (its folder under the runs root).")
@click.option("--batch-size", type=click.IntRange(min=1), default=None, help="Prompts per batch.")
@click.option("--distribution", default=None, help="Toolset distribution (`distributions`).")
@click.option("--model", default=None, help="Model to run (default: the profile's).")
@click.option("--base-url", default=None, help="Endpoint of the provider.")
@click.option(
    "--max-turns", type=click.IntRange(min=1), default=None, help="Tool rounds per prompt."
)
@click.option("--workers", type=click.IntRange(min=1), default=None, help="Worker processes.")
@click.option("--resume", is_flag=True, help="Also skip prompts already in the batch files.")
@click.option("--new-dataset", is_flag=True, help="Archive a run made with another dataset.")
@click.option("--seed", type=int, default=None, help="Seed for the toolset sampling.")
@click.option(
    "--reasoning-effort",
    default=None,
    help=f"One of: {', '.join(REASONING_CHOICES)} (default: the provider's).",
)
@click.option("--no-reasoning", is_flag=True, help="Switch reasoning off (sends effort none).")
@click.option("--prefill", "prefill_file", default=None, help="JSON array of prefill messages.")
@click.option("--ephemeral-system-prompt", default=None, help="Used in the run, not saved.")
@click.option("--providers-allowed", default=None, help="Comma list of providers to allow.")
@click.option("--providers-ignored", default=None, help="Comma list of providers to skip.")
@click.option("--providers-order", default=None, help="Comma list of providers to try in order.")
@click.option("--provider-sort", type=click.Choice(SORT_CHOICES), default=None)
@click.option("--max-samples", type=click.IntRange(min=1), default=None, help="First N prompts.")
@click.option("--verbose", is_flag=True, help="One line per prompt.")
def batch_command(
    dataset: Path,
    run_name: str,
    batch_size: int | None,
    distribution: str | None,
    model: str | None,
    base_url: str | None,
    max_turns: int | None,
    workers: int | None,
    resume: bool,
    new_dataset: bool,
    seed: int | None,
    reasoning_effort: str | None,
    no_reasoning: bool,
    prefill_file: str | None,
    ephemeral_system_prompt: str | None,
    providers_allowed: str | None,
    providers_ignored: str | None,
    providers_order: str | None,
    provider_sort: str | None,
    max_samples: int | None,
    verbose: bool,
) -> None:
    """Run DATASET (JSONL with a "prompt" per line) and write training samples."""
    effort: str | None = None
    if reasoning_effort is not None:
        effort = reasoning_effort.strip().lower()
        if effort not in REASONING_CHOICES:
            err(
                f"research batch: unknown reasoning effort {reasoning_effort!r}; "
                f"use one of: {', '.join(REASONING_CHOICES)}"
            )
            raise _exit(EXIT_USAGE)
    if no_reasoning:
        effort = "none"
    prefill = _prefill(prefill_file)
    settings = make_settings(
        dataset=dataset,
        run_name=run_name,
        batch_size=batch_size,
        distribution=distribution,
        model=model,
        base_url=base_url,
        max_turns=max_turns,
        workers=workers,
        resume=resume,
        new_dataset=new_dataset,
        seed=seed,
        reasoning_effort=effort,
        prefill=prefill,
        ephemeral_system_prompt=ephemeral_system_prompt,
        routing=_routing(providers_allowed, providers_ignored, providers_order, provider_sort),
        max_samples=max_samples,
        verbose=verbose,
    )
    code = run_batch(settings)
    if code:
        raise _exit(code)


def make_settings(**values: Any) -> BatchSettings:
    from indus.research.batch import BatchSettings

    return BatchSettings(**values)


def run_batch(settings: BatchSettings) -> int:
    """Run one batch (a seam the tests replace)."""
    from indus.research.batch import BatchRunner

    state = cli_state()
    runner = BatchRunner(
        state.scope,
        settings,
        out=click.echo,
        err=lambda line: click.echo(line, err=True),
    )
    return int(runner.run())


@research_group.command("distributions")
@click.argument("name", required=False)
def distributions_command(name: str | None) -> None:
    """List the toolset distributions (or one), most likely entries first."""
    from indus.research import distributions

    if name is not None:
        try:
            chosen = [distributions.get(name)]
        except distributions.UnknownDistribution as exc:
            err(f"research distributions: {exc}")
            raise _exit(EXIT_USAGE) from None
    else:
        chosen = [distributions.get(n) for n in sorted(distributions.names())]
    for index, dist in enumerate(chosen):
        if index:
            click.echo("")
        click.echo(f"{dist.name}: {dist.description}")
        for entry, percent in distributions.listing(dist):
            click.echo(f"  {entry:<18}{percent:>4}%")


def _later(name: str, task: str, help_text: str) -> click.Command:
    @click.command(
        name,
        help=f"{help_text} (not available yet).",
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    )
    def later() -> None:
        err(f"research {name}: {LATER.format(task=task)}")
        raise _exit(EXIT_FAILED)

    return later


research_group.add_command(
    _later("compress", "11..13", "Squeeze trajectories under a token budget")
)
research_group.add_command(_later("swe", "14", "Single-tool coding runner"))
research_group.add_command(_later("sample", "15", "Sample datasets and compress them"))
