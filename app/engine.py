"""Narrow agent boundary for the isolated Together Hermes profile."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Iterator, Protocol

from .config import (
    DEFAULT_HERMES_MODEL,
    DEFAULT_HERMES_PROFILE,
    DEFAULT_HERMES_PROVIDER,
    DEFAULT_HERMES_TOOLSETS,
)


@dataclass(frozen=True)
class EngineReply:
    content: str
    sources: tuple[str, ...] = ()
    generated_image_path: Path | None = None


@dataclass(frozen=True)
class EngineChunk:
    content: str
    generated_image_path: Path | None = None


class EngineError(RuntimeError): pass


class Engine(Protocol):
    def reply(self, *, room_id: str, prompt: str, image_path: Path | None = None) -> EngineReply: ...
    def stream_reply(self, *, room_id: str, prompt: str, image_path: Path | None = None) -> Iterator[EngineChunk]: ...


class HermesProfileEngine:
    """Invoke exactly one isolated profile; image paths originate only from app storage."""
    def __init__(self, *, hermes_executable: Path, hermes_home: Path, profile: str = DEFAULT_HERMES_PROFILE, model: str = DEFAULT_HERMES_MODEL, provider: str = DEFAULT_HERMES_PROVIDER, toolsets: tuple[str, ...] = DEFAULT_HERMES_TOOLSETS) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", profile) or profile.casefold() == "default":
            raise ValueError("Only a dedicated non-default Hermes profile may be invoked")
        if not model or not provider: raise ValueError("Hermes model and provider must be configured")
        self._hermes_executable, self._hermes_home, self._profile = Path(hermes_executable), Path(hermes_home), profile
        self._model, self._provider, self._toolsets = model, provider, toolsets

    @property
    def name(self) -> str: return "hermes-profile"

    def reply(self, *, room_id: str, prompt: str, image_path: Path | None = None) -> EngineReply:
        chunks = list(self.stream_reply(room_id=room_id, prompt=prompt, image_path=image_path))
        generated = next((chunk.generated_image_path for chunk in reversed(chunks) if chunk.generated_image_path), None)
        return EngineReply(content="".join(chunk.content for chunk in chunks), generated_image_path=generated)

    def stream_reply(self, *, room_id: str, prompt: str, image_path: Path | None = None) -> Iterator[EngineChunk]:
        del room_id
        command = [str(self._hermes_executable), "chat", "-q", prompt, "--oneshot", "--quiet", "--format", "stream-json", "--toolsets", ",".join(self._toolsets), "--provider", self._provider, "--model", self._model]
        if image_path is not None: command.extend(["--image", str(image_path)])
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1, cwd=self._profile_home, env=self._profile_env())
        except OSError as exc: raise EngineError("Unable to start the isolated Hermes profile") from exc
        assert process.stdout is not None
        try:
            generated_image = None
            for line in process.stdout:
                event = _json_event(line)
                if candidate := _generated_image_path(event, self._profile_home):
                    generated_image = candidate
                    continue
                if generated_image is None and (chunk := _stream_text_chunk(event)):
                    yield EngineChunk(content=chunk)
            if process.wait() != 0: raise EngineError("The isolated Hermes profile did not complete the request")
            if generated_image is not None:
                yield EngineChunk(content="Here you go — I made that image.", generated_image_path=generated_image)
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait()

    @property
    def _profile_home(self) -> Path: return self._hermes_home / "profiles" / self._profile
    def _profile_env(self) -> dict[str, str]:
        return {"HERMES_HOME":str(self._profile_home),"HERMES_BASE_HOME":str(self._hermes_home),"HOME":str(self._profile_home),"PATH":f"{self._hermes_executable.parent}:/usr/bin:/bin","PYTHONUTF8":"1","NO_COLOR":"1","TERM":"dumb"}


def _json_event(line: str) -> dict:
    try: event = json.loads(line)
    except json.JSONDecodeError: return {}
    return event if isinstance(event, dict) else {}


def _stream_text_chunk(event: dict) -> str:
    return event.get("text") if event.get("type") == "text" and isinstance(event.get("text"), str) else ""


def _generated_image_path(event: dict, profile_home: Path) -> Path | None:
    """Accept only successful image files created in Hermes' isolated image cache."""
    if event.get("type") != "tool_result" or event.get("name") != "image_generate":
        return None
    output = event.get("output")
    if isinstance(output, str):
        try: output = json.loads(output)
        except json.JSONDecodeError: return None
    if not isinstance(output, dict) or output.get("success") is not True or not isinstance(output.get("image"), str):
        return None
    candidate = Path(output["image"]).expanduser().resolve()
    cache_root = (profile_home / "cache" / "images").resolve()
    try: candidate.relative_to(cache_root)
    except ValueError: return None
    if candidate.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"} or not candidate.is_file(): return None
    return candidate


class FakeEngine:
    def reply(self, *, room_id: str, prompt: str, image_path: Path | None = None) -> EngineReply:
        del room_id, image_path
        return EngineReply(content=f"This is the Together prototype speaking. I received your shared note: “{prompt.strip()}”\n\nThe isolated Hermes adapter is intentionally not connected yet.")
    def stream_reply(self, *, room_id: str, prompt: str, image_path: Path | None = None) -> Iterator[EngineChunk]:
        yield EngineChunk(content=self.reply(room_id=room_id, prompt=prompt, image_path=image_path).content)
