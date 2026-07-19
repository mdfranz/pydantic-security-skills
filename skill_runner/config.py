"""Typed runner configuration and normalized model catalog loading."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Mapping, cast

import yaml


ThinkingEffort = Literal["low", "medium", "high", "xhigh"]
UiMode = Literal["console", "textual"]


@dataclass(frozen=True)
class RunOptions:
    """UI-neutral options passed from argparse into the application layer."""

    model: str
    workspace: Path
    task: str | None = None
    pristine: bool = False
    debug: bool = False
    logfire: bool = False
    interactive: bool = False
    thinking: ThinkingEffort | None = None
    max_tokens: int | None = None
    ui: UiMode = "console"
    max_retries: int = 5
    max_run_seconds: int | None = None
    max_turns: int | None = None

    @classmethod
    def from_namespace(cls, args: argparse.Namespace) -> RunOptions:
        return cls(
            model=args.model,
            workspace=Path(args.workspace),
            task=args.task,
            pristine=args.pristine,
            debug=args.debug,
            logfire=args.logfire,
            interactive=args.interactive,
            thinking=cast(ThinkingEffort | None, args.thinking),
            max_tokens=args.max_tokens,
            ui=cast(UiMode, args.ui),
            max_retries=args.max_retries,
            max_run_seconds=args.max_run_seconds,
            max_turns=args.max_turns,
        )

    def with_model(self, model: str) -> RunOptions:
        return replace(self, model=model)


@dataclass(frozen=True)
class ModelChoice:
    id: str
    alias: str
    description: str
    provider: str


@dataclass(frozen=True)
class ModelCatalog:
    """Canonical model list regardless of the YAML file's supported input shape."""

    default_model: str
    choices: tuple[ModelChoice, ...]

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> ModelCatalog:
        choices: list[ModelChoice] = []
        providers = config.get("providers")
        if isinstance(providers, Mapping):
            for provider_name, provider_info in providers.items():
                if not isinstance(provider_info, Mapping):
                    continue
                models = provider_info.get("models", [])
                if not isinstance(models, list):
                    continue
                choices.extend(_model_choices(models, provider=str(provider_name)))
        models = config.get("models", [])
        if isinstance(models, list):
            choices.extend(_model_choices(models))

        return cls(
            default_model=str(config.get("default_model", "google:gemini-3-flash-preview")),
            choices=tuple(choices),
        )

    def resolve(self, model_name: str) -> str:
        for choice in self.choices:
            if choice.alias == model_name or choice.id == model_name:
                return choice.id
        return model_name


def _model_choices(models: list[object], provider: str | None = None) -> list[ModelChoice]:
    choices: list[ModelChoice] = []
    for model in models:
        if not isinstance(model, Mapping):
            continue
        model_id = str(model.get("id", ""))
        if not model_id:
            continue
        choices.append(
            ModelChoice(
                id=model_id,
                alias=str(model.get("alias", model_id)),
                description=str(model.get("description", "")),
                provider=provider or (model_id.split(":", 1)[0] if ":" in model_id else ""),
            )
        )
    return choices


def load_model_catalog(project_root: Path) -> ModelCatalog:
    config_path = project_root / "models.yaml"
    if not config_path.exists():
        return ModelCatalog.from_mapping({})
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, Mapping):
            raise TypeError("top-level value must be a mapping")
        return ModelCatalog.from_mapping(loaded)
    except Exception as exc:
        print(f"Warning: Failed to load models.yaml: {exc}", file=sys.stderr)
        return ModelCatalog.from_mapping({})
