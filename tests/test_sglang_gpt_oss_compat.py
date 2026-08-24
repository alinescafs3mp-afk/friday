"""Contracts for the pinned SGLang GPT-OSS structured-output compatibility file."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "deploy" / "secondary-brain" / "windows-sglang"
PATCH = BUNDLE / "runtime" / "reasoner_grammar_backend.py"
PATCH_TARGET = "/sgl-workspace/sglang/python/sglang/srt/constrained/reasoner_grammar_backend.py"


class _BaseGrammarObject:
    def __init__(self) -> None:
        self._finished = False
        self.current_token: int | None = None


class _BaseGrammarBackend:
    @property
    def enable_strict_thinking(self) -> bool:
        return bool(getattr(self, "_enable_strict_thinking", False))


class _InvalidGrammarObject(_BaseGrammarObject):
    pass


class _TokenSequenceMatcher:
    def __init__(self, tokens: tuple[int, ...]) -> None:
        self.tokens = tuple(tokens)

    def __len__(self) -> int:
        return len(self.tokens)

    def advance(self, matched: int, token: int) -> int:
        candidate = self.tokens[:matched] + (token,)
        for length in range(min(len(candidate), len(self.tokens)), -1, -1):
            if candidate[-length:] == self.tokens[:length] if length else True:
                return length
        raise AssertionError("unreachable")


def _module(monkeypatch: Any) -> ModuleType:
    packages = (
        "sglang",
        "sglang.srt",
        "sglang.srt.constrained",
        "sglang.srt.parser",
        "sglang.srt.utils",
    )
    for name in packages:
        value = ModuleType(name)
        value.__path__ = []  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, name, value)

    torch = ModuleType("torch")
    torch.Tensor = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", torch)
    transformers = ModuleType("transformers")
    transformers.PreTrainedTokenizer = object  # type: ignore[attr-defined]
    transformers.PreTrainedTokenizerFast = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "transformers", transformers)

    environments = ModuleType("sglang.srt.environ")
    environments.envs = SimpleNamespace(SGLANG_MAX_THINK_TOKENS=SimpleNamespace(get=lambda: -1))
    monkeypatch.setitem(sys.modules, environments.__name__, environments)
    reasoning = ModuleType("sglang.srt.parser.reasoning_parser")
    reasoning.ReasoningParser = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, reasoning.__name__, reasoning)
    matcher = ModuleType("sglang.srt.utils.token_sequence_matcher")
    matcher.TokenSequenceMatcher = _TokenSequenceMatcher  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, matcher.__name__, matcher)
    base = ModuleType("sglang.srt.constrained.base_grammar_backend")
    base.BaseGrammarBackend = _BaseGrammarBackend  # type: ignore[attr-defined]
    base.BaseGrammarObject = _BaseGrammarObject  # type: ignore[attr-defined]
    base.InvalidGrammarObject = _InvalidGrammarObject  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, base.__name__, base)

    name = "sglang.srt.constrained.reasoner_grammar_backend"
    spec = importlib.util.spec_from_file_location(name, PATCH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


class _Grammar:
    def __init__(self) -> None:
        self.accepted: list[int] = []
        self.rollbacks: list[int] = []
        self.mask_calls = 0
        self.finished = False

    def accept_token(self, token: int) -> None:
        self.accepted.append(token)

    def rollback(self, count: int) -> None:
        self.rollbacks.append(count)

    def fill_vocab_mask(self, _mask: object, _index: int) -> None:
        self.mask_calls += 1

    def is_terminated(self) -> bool:
        return False

    def copy(self) -> _Grammar:
        copied = _Grammar()
        copied.accepted = list(self.accepted)
        return copied


def test_harmony_header_is_unmasked_until_final_message(monkeypatch: Any) -> None:
    module = _module(monkeypatch)
    grammar = _Grammar()
    wrapped = module.ReasonerGrammarObject(
        grammar=grammar,
        think_end_ids=[7, 8],
        content_start_ids=[3, 4, 5],
    )
    wrapped.maybe_init_reasoning(True)

    for token in (1, 7, 8, 9, 3, 6, 3, 4):
        wrapped.accept_token(token)
    assert wrapped._is_header()
    wrapped.fill_vocab_mask(object(), 0)
    assert grammar.accepted == []
    assert grammar.mask_calls == 0

    wrapped.accept_token(5)
    assert wrapped._is_generation()
    assert grammar.accepted == []
    wrapped.accept_token(42)
    assert grammar.accepted == [42]


def test_header_boundary_rollback_and_copy_preserve_match_state(monkeypatch: Any) -> None:
    module = _module(monkeypatch)
    wrapped = module.ReasonerGrammarObject(
        grammar=_Grammar(),
        think_end_ids=[7, 8],
        content_start_ids=[3, 4, 5],
    )
    wrapped.maybe_init_reasoning(True)
    for token in (7, 8, 3, 4, 5):
        wrapped.accept_token(token)
    assert wrapped._is_generation()

    wrapped.rollback(1)
    assert wrapped._is_header()
    copied = wrapped.copy()
    copied.accept_token(5)
    assert copied._is_generation()
    assert wrapped._is_header()


def test_non_harmony_detector_keeps_the_original_state_machine(monkeypatch: Any) -> None:
    module = _module(monkeypatch)

    class Tokenizer:
        def encode(self, marker: str, *, add_special_tokens: bool) -> list[int]:
            assert add_special_tokens is False
            return {"</think>": [7, 8]}[marker]

    detector = SimpleNamespace(think_end_token="</think>", think_excluded_tokens=None)
    backend = SimpleNamespace(is_support_token_filter=True)
    reasoner = module.ReasonerGrammarBackend(
        backend,
        SimpleNamespace(detector=detector),
        Tokenizer(),
    )
    assert reasoner.content_start_ids is None


def test_exact_gpt_oss_detector_enables_the_final_channel_marker(monkeypatch: Any) -> None:
    module = _module(monkeypatch)

    class Tokenizer:
        def encode(self, marker: str, *, add_special_tokens: bool) -> list[int]:
            assert add_special_tokens is False
            return {
                "<|end|>": [7],
                "<|channel|>final<|message|>": [3, 4, 5],
            }[marker]

    detector_type = type("GptOssDetector", (), {})
    detector_type.__module__ = "sglang.srt.parser.reasoning_parser"
    detector = detector_type()
    detector.think_end_token = "<|end|>"
    detector.think_excluded_tokens = None
    backend = SimpleNamespace(is_support_token_filter=True)

    reasoner = module.ReasonerGrammarBackend(
        backend,
        SimpleNamespace(detector=detector),
        Tokenizer(),
    )

    assert reasoner.content_start_ids == [3, 4, 5]


def test_compose_mounts_the_accepted_patch_over_the_pinned_module() -> None:
    compose = yaml.safe_load((BUNDLE / "compose.yml").read_text(encoding="utf-8"))
    rows = compose["services"]["sglang"]["volumes"]
    assert [row for row in rows if row.get("target") == PATCH_TARGET] == [
        {
            "type": "bind",
            "source": "./runtime/reasoner_grammar_backend.py",
            "target": PATCH_TARGET,
            "read_only": True,
        }
    ]


def test_launcher_verifies_patch_before_importing_sglang() -> None:
    launcher = (BUNDLE / "runtime" / "launch_sglang_secure.py").read_text(encoding="utf-8")
    assert "profile.sglang_compat_patch_sha256" in launcher
    assert launcher.index("_verify_sglang_compat_patch(") < launcher.index(
        "from sglang.launch_server import run_server"
    )
