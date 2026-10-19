"""Media tools: ``vision_analyze``, image and video generation, ``text_to_speech`` (P10-15..17).

``register_tools`` registers them on the module defaults. The provider registries are where
plugins add backends (P13): ``image_providers`` and ``video_providers`` (generation) and
``tts_providers`` (speech); they are the registries the default tools use. Speech-to-text is a
library (``media.stt``), not a model tool.
"""

from __future__ import annotations

from typing import Any

from indus.tools.registry import ToolRegistry

__all__ = ["register_tools"]


def register_tools(registry: ToolRegistry) -> None:
    from indus.tools.builtin.media import generate, tts, vision

    vision.DEFAULT_TOOL.register(registry)
    generate.DEFAULT_TOOLS.register(registry)
    tts.DEFAULT_TOOL.register(registry)


def __getattr__(name: str) -> Any:
    if name in ("image_providers", "video_providers"):
        from indus.tools.builtin.media import generate

        return getattr(generate.DEFAULT_TOOLS.providers, name)
    if name == "tts_providers":
        from indus.tools.builtin.media import tts

        return tts.DEFAULT_TOOL.providers
    raise AttributeError(name)
