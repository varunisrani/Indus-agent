"""P24-19: `indus pet` and the pet store, with a fake image service (no network, no art shipped)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from indus.foundation import config
from indus.foundation.scope import Scope
from indus.service import pets
from indus.service.pets import PetError, PetStore, PetTimeout, PetUnavailable
from indus.tools.builtin.media.providers import ImageCapabilities, ImageRequest, MediaResult
from tests.fakes.media_providers import PNG_BYTES, FakeImageProvider
from tests.unit.cli.commands.cmdrun import run_cli


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeImageProvider:
    provider = FakeImageProvider(capabilities=ImageCapabilities(edit=True))
    monkeypatch.setattr(pets, "image_provider", lambda scope: provider)
    return provider


def _generate(scope: Scope, idea: str = "a sleepy otter", **kw: object) -> pets.DraftSet:
    return asyncio.run(pets.generate_drafts(scope, idea, **kw))  # type: ignore[arg-type]


def _hatched(scope: Scope, name: str = "Otto") -> pets.Pet:
    made = _generate(scope, count=1)
    return asyncio.run(pets.hatch(scope, made.token, 0, name))


# -- the store and the drawing steps --------------------------------------------------------------


def test_deadlines_are_the_rows_numbers() -> None:
    assert pets.GENERATE_TIMEOUT_S == 420.0
    assert pets.HATCH_TIMEOUT_S == 3_600.0


def test_generate_writes_drafts_with_our_prompt(scope: Scope, fake: FakeImageProvider) -> None:
    made = _generate(scope, "  a   tiny  fox ", count=9)  # clamped to four
    assert [d.index for d in made.drafts] == [0, 1, 2, 3]
    assert made.idea == "a tiny fox"
    assert all(d.path.read_bytes() == PNG_BYTES for d in made.drafts)
    assert made.directory.parent == pets.drafts_root(scope)
    assert len(fake.requests) == 4
    assert all("a tiny fox" in r.prompt and r.aspect_ratio == "square" for r in fake.requests)
    assert fake.requests[0].model == "fake-model-1"


def test_generate_timeout_is_honoured_and_leaves_nothing(
    scope: Scope, fake: FakeImageProvider
) -> None:
    fake.hold = asyncio.Event()  # never set: the service never answers
    with pytest.raises(PetTimeout, match="0.05 s"):
        _generate(scope, timeout_s=0.05)
    root = pets.drafts_root(scope)
    assert not root.exists() or list(root.iterdir()) == []


def test_generate_uses_the_420_s_deadline_by_default(
    scope: Scope, fake: FakeImageProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[float] = []
    real = asyncio.timeout

    def spy(delay: float | None) -> asyncio.Timeout:
        seen.append(delay or 0.0)
        return real(delay)

    monkeypatch.setattr(pets.asyncio, "timeout", spy)
    _generate(scope, count=1)
    assert seen == [420.0]


def test_generate_refuses_non_images_and_links(scope: Scope, fake: FakeImageProvider) -> None:
    fake.result = lambda request: MediaResult(data=b"<html>not a picture</html>")
    with pytest.raises(PetError, match="not a PNG"):
        _generate(scope, count=1)
    fake.result = lambda request: MediaResult(url="https://images.example/x.png")
    with pytest.raises(PetError, match="link instead of a picture"):
        _generate(scope, count=1)


def test_one_bad_draft_keeps_the_good_ones(scope: Scope, fake: FakeImageProvider) -> None:
    def answer(request: ImageRequest) -> MediaResult:
        bad = "ink outline" in request.prompt
        return MediaResult(data=b"nope" if bad else PNG_BYTES)

    fake.result = answer
    made = _generate(scope, count=4)
    assert [d.index for d in made.drafts] == [0, 1, 3]


def test_generate_without_an_idea_or_a_service(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(PetError, match="Describe the pet"):
        asyncio.run(pets.generate_drafts(scope, "   "))
    with pytest.raises(PetUnavailable, match="image service"):
        asyncio.run(pets.generate_drafts(scope, "an owl"))  # no key for the built-in service


def test_hatch_draws_every_mood_after_the_draft(scope: Scope, fake: FakeImageProvider) -> None:
    made = _generate(scope, count=2)
    fake.requests.clear()
    pet = asyncio.run(pets.hatch(scope, made.token, 1, "Otto the Otter"))
    assert pet.slug == "otto-the-otter" and pet.name == "Otto the Otter"
    assert set(pet.moods) == set(pets.MOODS)
    assert len(fake.requests) == len(pets.MOODS)
    assert all(r.image is not None and r.image.data == PNG_BYTES for r in fake.requests)
    assert all("a sleepy otter" in r.prompt for r in fake.requests)
    manifest = json.loads((pet.directory / "pet.json").read_text())
    assert manifest["name"] == "Otto the Otter" and manifest["idea"] == "a sleepy otter"
    store = PetStore(scope)
    assert store.shown() is None  # hatched, not shown
    assert [p.slug for p in store.list()] == ["otto-the-otter"]


def test_hatch_with_reference_only_services(scope: Scope, fake: FakeImageProvider) -> None:
    fake.capabilities = ImageCapabilities(max_reference_images=1)
    pet = _hatched(scope)
    assert all(r.image is None and len(r.references) == 1 for r in fake.requests[-6:])
    assert pet.slug == "otto"


def test_hatch_timeout_and_bad_tokens(scope: Scope, fake: FakeImageProvider) -> None:
    made = _generate(scope, count=1)
    with pytest.raises(pets.PetNotFound):
        asyncio.run(pets.hatch(scope, "../../etc", 0, "X"))
    with pytest.raises(pets.PetNotFound, match="no draft 3"):
        asyncio.run(pets.hatch(scope, made.token, 3, "X"))
    with pytest.raises(PetError, match="letter or digit"):
        asyncio.run(pets.hatch(scope, made.token, 0, "!!!"))
    fake.hold = asyncio.Event()
    with pytest.raises(PetTimeout):
        asyncio.run(pets.hatch(scope, made.token, 0, "Slow", timeout_s=0.05))
    assert PetStore(scope).list() == []


def test_drafts_are_swept_after_an_hour(scope: Scope, fake: FakeImageProvider) -> None:
    old = _generate(scope, count=1)
    asyncio.run(pets.generate_drafts(scope, "new", count=1, now=lambda: 10**12))
    assert not old.directory.exists()


def test_use_rename_remove_follow_the_shown_pet(scope: Scope, fake: FakeImageProvider) -> None:
    pet = _hatched(scope)
    store = PetStore(scope)
    store.use(pet.slug)
    assert store.shown() is not None and store.settings().active == "otto"
    renamed = store.rename("otto", "Captain Otto")
    assert renamed.slug == "captain-otto" and not (pets.pets_root(scope) / "otto").exists()
    assert store.settings().active == "captain-otto" and store.settings().enabled
    second = _hatched(scope, "Captain Otto")
    assert second.slug == "captain-otto-2"
    store.remove("captain-otto")
    assert store.settings() == pets.PetSettings(enabled=False, active="", scale=1.0)
    store.use(second.slug)
    store.remove(second.slug)
    assert store.list() == []


def test_scale_is_clamped(scope: Scope) -> None:
    store = PetStore(scope)
    assert store.set_scale(99) == pets.SCALE_MAX
    assert store.set_scale(0) == pets.SCALE_MIN
    assert config.load(scope).get("pet.scale") == pets.SCALE_MIN


def test_sprite_payload_has_a_data_uri_per_mood(scope: Scope, fake: FakeImageProvider) -> None:
    pet = _hatched(scope)
    payload = pets.sprite_payload(pet, PetStore(scope).settings())
    assert set(payload["moods"]) == set(pets.MOODS)
    assert all(uri.startswith("data:image/png;base64,") for uri in payload["moods"].values())
    again = pets.sprite_payload(pet, PetStore(scope).settings())
    assert again["revision"] == payload["revision"]


def test_a_broken_manifest_is_not_a_pet(scope: Scope) -> None:
    folder = pets.pets_root(scope) / "ghost"
    folder.mkdir(parents=True)
    (folder / "pet.json").write_text('{"name": "Ghost", "moods": {"rest": "../x.png"}}')
    assert PetStore(scope).list() == []


# -- the command ----------------------------------------------------------------------------------


def test_cli_generate_then_hatch_then_use(
    capsys: pytest.CaptureFixture[str], scope: Scope, fake: FakeImageProvider
) -> None:
    ran = run_cli(capsys, scope, "pet", "generate", "a", "small", "owl", "-n", "2")
    assert ran.code == 0, ran.err
    token = ran.out.split("token ")[1].split(",")[0]
    assert "  0: " in ran.out and "  1: " in ran.out
    hatched = run_cli(
        capsys, scope, "pet", "hatch", token, "--pick", "1", "--name", "Hoot", "--use"
    )
    assert hatched.code == 0, hatched.err
    assert "Hatched Hoot (hoot)" in hatched.out
    shown = run_cli(capsys, scope, "pet", "show")
    assert shown.code == 0 and "Hoot (hoot)  — shown" in shown.out and "cheer" in shown.out
    listed = run_cli(capsys, scope, "pet", "list")
    assert listed.out.strip() == "* hoot  Hoot"


def test_cli_generate_timeout_exits_1(
    capsys: pytest.CaptureFixture[str],
    scope: Scope,
    fake: FakeImageProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake.hold = asyncio.Event()
    real = pets.generate_drafts

    async def quick(scope: Scope, idea: str, **kw: object) -> pets.DraftSet:
        return await real(scope, idea, timeout_s=0.05, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(pets, "generate_drafts", quick)
    ran = run_cli(capsys, scope, "pet", "generate", "owl")
    assert ran.code == 1 and "did not finish the drafts within 0.05 s" in ran.err


def test_cli_rename_remove_and_guidance(
    capsys: pytest.CaptureFixture[str], scope: Scope, fake: FakeImageProvider
) -> None:
    _hatched(scope, "Pip")
    assert run_cli(capsys, scope, "pet", "rename", "pip", "Pip", "Two").out.strip() == (
        "Renamed to Pip Two (pip-two)."
    )
    refused = run_cli(capsys, scope, "pet", "remove", "pip-two")
    assert refused.code == 1 and "needs a terminal to confirm, or -y" in refused.err
    assert run_cli(capsys, scope, "pet", "remove", "pip-two", "-y").code == 0
    missing = run_cli(capsys, scope, "pet", "use", "pip-two")
    assert missing.code == 1 and "No pet named" in missing.err
    assert run_cli(capsys, scope, "pet", "off").code == 0
    nothing = run_cli(capsys, scope, "pet", "show")
    assert nothing.code == 1 and "No pet is shown" in nothing.err


def test_cli_list_on_a_fresh_profile(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = run_cli(capsys, scope, "pet", "list")
    assert ran.code == 0 and "No pets yet" in ran.out


def test_no_reference_art_ships() -> None:
    package = Path(pets.__file__).parent
    assert not list(package.glob("**/*.png")) and not list(package.glob("**/*.webp"))
