from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
import shutil

from i18n import get_app_config


@dataclass(frozen=True)
class EngineDefinition:
    id: int
    name: str
    command: str

    @property
    def command_lower(self) -> str:
        return (self.command or '').strip().lower()

    @property
    def is_firefox(self) -> bool:
        return 'firefox' in self.command_lower

    @property
    def is_chromium_family(self) -> bool:
        return 'chrome' in self.command_lower or 'chromium' in self.command_lower

    @property
    def supports_adblock(self) -> bool:
        return self.is_firefox

    @property
    def supports_background_mode(self) -> bool:
        return self.is_firefox


def _command_candidates(command: str) -> list[str]:
    command = (command or '').strip().lower()
    candidates: list[str] = []
    if command:
        candidates.append(command)
    if 'firefox' in command:
        candidates.extend(['firefox', 'firefox-esr'])
    elif 'chrome' in command:
        candidates.extend(['google-chrome', 'google-chrome-stable', 'chrome', 'chromium', 'chromium-browser'])
    elif 'chromium' in command:
        candidates.extend(['chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable', 'chrome'])
    seen: set[str] = set()
    return [candidate for candidate in candidates if candidate and not (candidate in seen or seen.add(candidate))]


def engine_available(engine: dict | EngineDefinition) -> bool:
    command = engine.command if isinstance(engine, EngineDefinition) else engine.get('command', '')
    return any(shutil.which(candidate) for candidate in _command_candidates(command))


def configured_engines() -> list[EngineDefinition]:
    config = get_app_config()
    raw_engines = config.get('engines', []) or [
        {'id': 1, 'name': 'Firefox', 'command': 'firefox'},
        {'id': 2, 'name': 'Chrome', 'command': 'google-chrome'},
    ]
    return [EngineDefinition(int(engine['id']), engine['name'], engine.get('command', '')) for engine in raw_engines]


_AVAILABLE_ENGINES_CACHE: list[dict] | None = None


def available_engines() -> list[dict]:
    global _AVAILABLE_ENGINES_CACHE
    if _AVAILABLE_ENGINES_CACHE is None:
        _AVAILABLE_ENGINES_CACHE = [engine.__dict__.copy() for engine in configured_engines() if engine_available(engine)]
    return [dict(engine) for engine in _AVAILABLE_ENGINES_CACHE]


def engine_icon_name(engine_name: str) -> str:
    engine_name = (engine_name or '').lower()
    if 'firefox' in engine_name:
        return 'firefox'
    if 'chrome' in engine_name:
        return 'google-chrome'
    if 'chromium' in engine_name:
        return 'chromium-browser'
    return 'applications-internet-symbolic'
