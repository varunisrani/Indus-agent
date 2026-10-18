import json
from pathlib import Path

import pytest

from tests.fakes.model import (
    Disconnect,
    Fail,
    Finish,
    Reasoning,
    Script,
    ScriptError,
    Stall,
    Text,
    ToolCall,
    Turn,
    Usage,
    load_script,
)
from tests.fakes.model.mini_yaml import YamlSubsetError, loads


def test_hello_loads_to_one_text_turn(fake_model_script) -> None:
    script = fake_model_script("hello")
    assert script == Script((Turn((Text(("hel", "l", "o")), Usage(10, 3))),))
    assert "".join(script.turns[0].steps[0].chunks) == "hello"


def test_every_step_kind_loads(tmp_path: Path) -> None:
    path = tmp_path / "all.yaml"
    path.write_text(
        """\
turns:
  - steps:
      - reasoning: thinking
      - text: ["a", "b"]
      - tool_call:
          name: read_file
          arguments: {path: "notes.txt", limit: 5}
          id: call-1
          fragments: 2
      - tool_call: {name: broken, arguments: '{"path": '}
      - usage: {prompt: 7, completion: 2, cached: 4}
      - finish: tool_calls
  - steps:
      - fail:
          status: 429
          body: slow down
          headers: {retry-after: "2"}
  - steps:
      - text: partial
      - stall: 30
  - steps:
      - text: cut
      - disconnect:
""",
        encoding="utf-8",
    )
    script = load_script(path)
    assert script.turns == (
        Turn(
            (
                Reasoning(("thinking",)),
                Text(("a", "b")),
                ToolCall("read_file", {"path": "notes.txt", "limit": 5}, "call-1", 2),
                ToolCall("broken", '{"path": '),
                Usage(7, 2, 4),
                Finish("tool_calls"),
            )
        ),
        Turn((Fail(429, "slow down", {"retry-after": "2"}),)),
        Turn((Text(("partial",)), Stall(30.0))),
        Turn((Text(("cut",)), Disconnect())),
    )


def test_unknown_step_kind_names_the_line(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "turns:\n  - steps:\n      - text: hi\n      - shout: HI\n",
        encoding="utf-8",
    )
    with pytest.raises(ScriptError) as exc:
        load_script(path)
    message = str(exc.value)
    assert f"{path}:4:" in message
    assert "'shout'" in message
    assert "known:" in message


def test_bad_step_value_names_the_line(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("turns:\n  - steps:\n      - usage: {prompt: 1}\n", encoding="utf-8")
    with pytest.raises(ScriptError, match=r":3: bad 'usage' step: missing field"):
        load_script(path)


def test_json_script_loads_and_names_step_on_error(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"turns": [{"steps": [{"text": "hi"}, {"finish": "stop"}]}]}))
    assert load_script(good) == Script((Turn((Text(("hi",)), Finish("stop"))),))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"turns": [{"steps": [{"text": "hi"}, {"nope": 1}]}]}))
    with pytest.raises(ScriptError, match=r"turn 1, step 2.*'nope'"):
        load_script(bad)


def test_missing_turns_and_bad_shapes(tmp_path: Path) -> None:
    path = tmp_path / "x.yaml"
    path.write_text("steps: []\n", encoding="utf-8")
    with pytest.raises(ScriptError, match="'turns'"):
        load_script(path)
    path.write_text("turns:\n  - steps:\n      - text: a\n        usage: x\n", encoding="utf-8")
    with pytest.raises(ScriptError, match=":3: a step is a mapping with exactly one key"):
        load_script(path)


def test_unsupported_yaml_is_rejected_with_line(tmp_path: Path) -> None:
    path = tmp_path / "x.yaml"
    path.write_text("turns:\n  - steps:\n      - text: |\n          block\n", encoding="utf-8")
    with pytest.raises(ScriptError, match=":3: unsupported YAML syntax"):
        load_script(path)


def test_mini_yaml_scalars_and_nesting() -> None:
    doc = loads(
        """\
# comment
a: 1
b: 2.5
c: "quoted # not a comment"
d: 'it''s'
e: ~
f: true
g:
- x
- [1, "two", {k: v}]
h: plain text  # trailing comment
"""
    )
    assert doc == {
        "a": 1,
        "b": 2.5,
        "c": "quoted # not a comment",
        "d": "it's",
        "e": None,
        "f": True,
        "g": ["x", [1, "two", {"k": "v"}]],
        "h": "plain text",
    }


def test_mini_yaml_errors() -> None:
    with pytest.raises(YamlSubsetError, match="line 2"):
        loads("a: 1\n    b: 2\n")
    with pytest.raises(YamlSubsetError, match="duplicate key"):
        loads("a: 1\na: 2\n")
    with pytest.raises(YamlSubsetError, match="line 1"):
        loads("a: [1, 2\n")
