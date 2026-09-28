"""Configuration for the Together service and its isolated Hermes adapter."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
import shutil

DEFAULT_HERMES_MODEL = "gpt-6-luna"
DEFAULT_HERMES_PROVIDER = "openai-codex"
DEFAULT_HERMES_PROFILE = "together"
DEFAULT_HERMES_TOOLSETS = ("web", "vision", "image_gen", "memory")
_USERNAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_PROFILE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class ParticipantSettings:
    username: str
    display_name: str
    password: str


@dataclass(frozen=True)
class Settings:
    db_path: Path
    static_dir: Path
    upload_dir: Path
    session_secret: str
    participants: tuple[ParticipantSettings, ...]
    cookie_secure: bool
    hermes_home: Path
    hermes_executable: Path
    default_room_title: str = "Shared conversation"
    hermes_profile: str = DEFAULT_HERMES_PROFILE
    hermes_model: str = DEFAULT_HERMES_MODEL
    hermes_provider: str = DEFAULT_HERMES_PROVIDER
    hermes_toolsets: tuple[str, ...] = DEFAULT_HERMES_TOOLSETS

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(__file__).resolve().parent.parent
        runtime_dir = Path(os.environ.get("TOGETHER_RUNTIME_DIR", root / ".runtime")).expanduser()
        hermes_home = Path(os.environ.get("TOGETHER_HERMES_HOME", root / ".engine-home")).expanduser()
        executable = os.environ.get("TOGETHER_HERMES_EXECUTABLE") or shutil.which("hermes") or "hermes"
        executable_path = Path(executable).expanduser()
        if not executable_path.is_file() and (located := shutil.which(str(executable_path))):
            executable_path = Path(located)
        session_secret = os.environ.get("TOGETHER_SESSION_SECRET", "")
        if len(session_secret) < 32:
            raise ValueError("TOGETHER_SESSION_SECRET must be at least 32 characters")
        participants = tuple(
            ParticipantSettings(
                username=os.environ.get(f"TOGETHER_USER_{index}_USERNAME", f"member{index}"),
                display_name=os.environ.get(f"TOGETHER_USER_{index}_DISPLAY_NAME", f"Participant {index}"),
                password=os.environ.get(f"TOGETHER_USER_{index}_PASSWORD", ""),
            )
            for index in (1, 2)
        )
        toolsets = tuple(
            item.strip()
            for item in os.environ.get("TOGETHER_HERMES_TOOLSETS", ",".join(DEFAULT_HERMES_TOOLSETS)).split(",")
            if item.strip()
        )
        return cls(
            db_path=Path(os.environ.get("TOGETHER_DB_PATH", runtime_dir / "together.db")).expanduser(),
            static_dir=root / "static",
            upload_dir=Path(os.environ.get("TOGETHER_UPLOAD_DIR", runtime_dir / "uploads")).expanduser(),
            session_secret=session_secret,
            participants=participants,
            cookie_secure=os.environ.get("TOGETHER_COOKIE_SECURE", "true").lower() in {"1", "true", "yes"},
            hermes_home=hermes_home,
            hermes_executable=executable_path,
            default_room_title=os.environ.get("TOGETHER_DEFAULT_ROOM_TITLE", "Shared conversation").strip(),
            hermes_profile=os.environ.get("TOGETHER_HERMES_PROFILE", DEFAULT_HERMES_PROFILE).strip(),
            hermes_model=os.environ.get("TOGETHER_HERMES_MODEL", DEFAULT_HERMES_MODEL).strip(),
            hermes_provider=os.environ.get("TOGETHER_HERMES_PROVIDER", DEFAULT_HERMES_PROVIDER).strip(),
            hermes_toolsets=toolsets,
        )

    def validate_bootstrap(self) -> None:
        if len(self.session_secret) < 32:
            raise ValueError("TOGETHER_SESSION_SECRET must be at least 32 characters")
        if len(self.participants) != 2:
            raise ValueError("TogetherAI requires exactly two configured participants")
        usernames = [participant.username.casefold() for participant in self.participants]
        if len(set(usernames)) != 2 or any(not _USERNAME.fullmatch(name) for name in usernames):
            raise ValueError("Participant usernames must be distinct and use letters, numbers, '.', '_' or '-'")
        for index, participant in enumerate(self.participants, start=1):
            if not participant.display_name.strip() or len(participant.display_name) > 80:
                raise ValueError(f"TOGETHER_USER_{index}_DISPLAY_NAME must contain 1–80 characters")
            if not 12 <= len(participant.password) <= 512:
                raise ValueError(f"TOGETHER_USER_{index}_PASSWORD must be 12–512 characters")
        if not self.default_room_title or len(self.default_room_title) > 120:
            raise ValueError("TOGETHER_DEFAULT_ROOM_TITLE must contain 1–120 characters")
        if not _PROFILE.fullmatch(self.hermes_profile) or self.hermes_profile.casefold() == "default":
            raise ValueError("TOGETHER_HERMES_PROFILE must name a dedicated non-default Hermes profile")
        if not self.hermes_model or not self.hermes_provider:
            raise ValueError("TOGETHER_HERMES_MODEL and TOGETHER_HERMES_PROVIDER are required")
        if not self.hermes_toolsets or any(not re.fullmatch(r"[A-Za-z0-9_-]+", item) for item in self.hermes_toolsets):
            raise ValueError("TOGETHER_HERMES_TOOLSETS must be a comma-separated list of toolset names")
        profiles_dir = (self.hermes_home / "profiles").resolve()
        profile_path = self.hermes_home / "profiles" / self.hermes_profile
        if profile_path.is_symlink():
            raise ValueError("The configured Hermes profile must not be a symlink")
        profile_dir = profile_path.resolve()
        if profile_dir.parent != profiles_dir or not profile_dir.is_dir():
            raise ValueError("The configured isolated Hermes profile is unavailable")
        executable = self.hermes_executable
        if not executable.is_file():
            located = shutil.which(str(executable))
            if located is None:
                raise ValueError("TOGETHER_HERMES_EXECUTABLE must point to an available Hermes executable")
