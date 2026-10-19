"""``indus research``: turn the agent into a data engine (P22-09, P22-11..15).

Verbs: ``batch DATASET --run-name NAME …`` runs a JSONL of prompts on a process pool and writes
training samples (``indus.research.batch``); ``distributions [NAME]`` lists the toolset
distributions, most likely entries first; ``compress INPUT`` squeezes trajectories under a token
budget (``indus.research.compress_runner``; timed-out entries go to a rejects file);
``swe --task TEXT | --prompts FILE`` runs coding tasks with only a terminal tool
(``indus.research.swe_runner``); ``sample --dataset NAME …`` draws long trajectories from a
required dataset list, compresses and merges them (``indus.research.sample``). ``compress``,
``swe`` and ``sample`` exit 2 on usage errors (a missing input, no task, no dataset list, a
tokenizer that cannot load) and 1 on runtime failures.

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

from indus.cli.cmdkit import cli_state, config_of, err, group

if TYPE_CHECKING:
    from indus.research.batch import BatchSettings

__all__ = ["REASONING_CHOICES", "SORT_CHOICES", "research_group", "split_csv"]

REASONING_CHOICES = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
SORT_CHOICES = ("price", "throughput", "latency")
EXIT_FAILED = 1
EXIT_USAGE = 2


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


# -- compress (P22-11..13) -------------------------------------------------------------------------


def tokenizer_loader() -> Any:
    """The tokenizer loader (``AutoTokenizer.from_pretrained``; a seam the tests replace)."""
    from indus.research.compress_config import transformers_loader

    return transformers_loader()


def summariser_for(scope: Any, config: Any) -> Any:
    """The summariser of a compress run (a seam the tests replace); made inside the run's loop."""
    from indus.research.compressor import aux_summariser

    return aux_summariser(scope, config.summarization)


def _echo_err(line: str) -> None:
    click.echo(line, err=True)


@research_group.command("compress")
@click.argument("input_path", metavar="INPUT", type=click.Path(path_type=Path))
@click.option("--output", type=click.Path(path_type=Path), default=None, help="Output file/folder.")
@click.option(
    "--config",
    "config_file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="YAML settings (default: the shipped defaults).",
)
@click.option("--target-max-tokens", type=click.IntRange(min=1), default=None)
@click.option("--tokenizer", "tokenizer_name", default=None, help="Tokenizer repository.")
@click.option("--revision", default=None, help="Tokenizer revision (commit) to load.")
@click.option(
    "--trust-remote-code",
    is_flag=True,
    help="Let the tokenizer repository run its own code (off by default).",
)
@click.option("--sample-percent", type=float, default=None, help="Keep this share of entries.")
@click.option("--seed", type=int, default=42, show_default=True, help="Seed for sampling.")
@click.option("--dry-run", is_flag=True, help="Say what would happen; write nothing.")
def compress_command(
    input_path: Path,
    output: Path | None,
    config_file: Path | None,
    target_max_tokens: int | None,
    tokenizer_name: str | None,
    revision: str | None,
    trust_remote_code: bool,
    sample_percent: float | None,
    seed: int,
    dry_run: bool,
) -> None:
    """Squeeze the trajectories of INPUT (a .jsonl file or a folder of them) under a token
    budget. Timed-out entries go to a rejects file next to the output."""
    from indus.research import compress_config, compress_runner

    try:
        config = compress_config.load_config(config_file)
    except compress_config.ConfigError as exc:
        err(f"research compress: {exc}")
        raise _exit(EXIT_USAGE) from None
    tokenizer: dict[str, Any] = {}
    if tokenizer_name:
        tokenizer["name"] = tokenizer_name
    if revision:
        tokenizer["revision"] = revision
    if trust_remote_code:
        tokenizer["trust_remote_code"] = True
    if tokenizer:
        config = config.model_copy(
            update={"tokenizer": config.tokenizer.model_copy(update=tokenizer)}
        )
    if target_max_tokens is not None:
        config = config.model_copy(
            update={
                "compression": config.compression.model_copy(
                    update={"target_max_tokens": target_max_tokens}
                )
            }
        )
    scope = cli_state().scope
    settings = compress_runner.RunSettings(
        input_path, output, sample_percent=sample_percent, seed=seed, dry_run=dry_run
    )

    def counter() -> Any:
        return compress_config.load_tokenizer(
            config.tokenizer, loader=tokenizer_loader(), warn=_echo_err
        )

    code = compress_runner.run(
        settings,
        config,
        counter=counter,
        make_summariser=lambda: summariser_for(scope, config),
        out=click.echo,
        err=_echo_err,
    )
    if code:
        raise _exit(code)


# -- swe (P22-14) ----------------------------------------------------------------------------------


def swe_chat(scope: Any, settings: Any) -> tuple[Any, str]:
    """The model caller of a SWE run and its model (a seam the tests replace)."""
    from indus.research.swe_runner import provider_chat

    return provider_chat(scope, settings)


def swe_environments(scope: Any, settings: Any) -> Any:
    """One environment per task from the backend registry (a seam the tests replace)."""
    from indus.research.swe_runner import RegistryEnvironments

    return RegistryEnvironments(scope, settings)


@research_group.command("swe")
@click.option("--task", default=None, help="One task to run.")
@click.option(
    "--prompts",
    "prompts_file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help='Tasks, one per line: {"prompt": ...}, {"task": ...} or plain text.',
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="JSONL file the trajectories are appended to (default: swe-trajectories.jsonl).",
)
@click.option(
    "--env",
    "env_name",
    type=click.Choice(["local", "docker", "modal"]),
    default="local",
    show_default=True,
)
@click.option("--image", default=None, help="Container image (default: the profile's).")
@click.option("--cwd", default=None, help="Working folder (default: a temp folder, or /tmp).")
@click.option("--model", default=None, help="Model to run (default: the profile's).")
@click.option("--provider", default=None, help="Provider (default: the profile's).")
@click.option("--base-url", default=None, help="Endpoint of the provider.")
@click.option("--max-iterations", type=click.IntRange(min=1), default=15, show_default=True)
@click.option(
    "--command-timeout",
    type=click.FloatRange(min=0, min_open=True),
    default=60.0,
    show_default=True,
    help="Seconds a command may take unless the call names its own timeout.",
)
def swe_command(
    task: str | None,
    prompts_file: Path | None,
    output: Path | None,
    env_name: str,
    image: str | None,
    cwd: str | None,
    model: str | None,
    provider: str | None,
    base_url: str | None,
    max_iterations: int,
    command_timeout: float,
) -> None:
    """Run coding tasks with only a terminal tool and write their trajectories."""
    import asyncio

    from indus.foundation.errors import IndusError
    from indus.research import swe_runner

    if (task is None) == (prompts_file is None):
        err("research swe: give exactly one of --task TEXT or --prompts FILE")
        raise _exit(EXIT_USAGE)
    if task is not None:
        tasks = [task]
    else:
        assert prompts_file is not None
        try:
            tasks = swe_runner.load_prompts(prompts_file)
        except swe_runner.PromptError as exc:
            err(f"research swe: {exc}")
            raise _exit(EXIT_USAGE) from None
        if not tasks:
            err(f"research swe: no tasks in {prompts_file}")
            raise _exit(EXIT_USAGE)
    settings = swe_runner.SweSettings(
        env=env_name,
        image=image,
        cwd=cwd,
        model=model,
        provider=provider,
        base_url=base_url,
        max_iterations=max_iterations,
        command_timeout=command_timeout,
    )
    scope = cli_state().scope
    try:
        chat, model_name = swe_chat(scope, settings)
    except IndusError as exc:
        err(f"research swe: {exc.message}")
        raise _exit(EXIT_FAILED) from None
    runner = swe_runner.SweRunner(
        scope,
        settings,
        chat=chat,
        environments=swe_environments(scope, settings),
        model=model_name,
        out=click.echo,
    )
    target = output or Path(swe_runner.DEFAULT_OUTPUT)
    results = asyncio.run(runner.run_all(tasks, target))
    done = sum(1 for r in results if r["completed"])
    click.echo(f"{done} of {len(results)} task(s) completed; trajectories appended to {target}")


# -- sample (P22-15) -------------------------------------------------------------------------------


def dataset_loader() -> Any:
    """How datasets are loaded (Hugging Face ``datasets``; a seam the tests replace)."""
    from indus.research.sample import hf_loader

    return hf_loader


@research_group.command("sample")
@click.option("--dataset", "datasets", multiple=True, help="Dataset to draw from (repeatable).")
@click.option("--samples", type=click.IntRange(min=1), default=2500, show_default=True)
@click.option("--min-tokens", type=click.IntRange(min=0), default=16000, show_default=True)
@click.option("--seed", type=int, default=42, show_default=True)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    help="Folder for the merged file and the work folder (default: here).",
)
@click.option("--name", default="compressed_samples", show_default=True)
@click.option(
    "--config",
    "config_file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Compression settings (default: the shipped defaults).",
)
def sample_command(
    datasets: tuple[str, ...],
    samples: int,
    min_tokens: int,
    seed: int,
    out_dir: Path,
    name: str,
    config_file: Path | None,
) -> None:
    """Draw long trajectories from datasets, compress them and merge them into one file. The
    dataset list is required (--dataset or research.sample_datasets)."""
    from indus.research import compress_config, compress_runner, sample

    scope = cli_state().scope
    configured = config_of(scope).get(sample.CONFIG_KEY)
    try:
        names = sample.dataset_list(datasets, configured)
        config = compress_config.load_config(config_file)
    except (sample.SampleError, compress_config.ConfigError) as exc:
        err(f"research sample: {exc}")
        raise _exit(EXIT_USAGE) from None
    loaded: list[Any] = []

    def counter() -> Any:
        if not loaded:
            loaded.append(
                compress_config.load_tokenizer(
                    config.tokenizer, loader=tokenizer_loader(), warn=_echo_err
                )
            )
        return loaded[0]

    def compress(source: Path, target: Path) -> int:
        return compress_runner.run(
            compress_runner.RunSettings(source, target),
            config,
            counter=counter,
            make_summariser=lambda: summariser_for(scope, config),
            out=click.echo,
            err=_echo_err,
        )

    settings = sample.SampleSettings(
        names, out_dir, name=name, samples=samples, min_tokens=min_tokens, seed=seed
    )
    code = sample.run_sample(
        settings,
        loader=dataset_loader(),
        counter=counter,
        compress=compress,
        out=click.echo,
        err=_echo_err,
    )
    if code:
        raise _exit(code)
