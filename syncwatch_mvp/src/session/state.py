from dataclasses import dataclass, field

from src.media.metadata import MediaInfo
from src.session.role import Role


@dataclass(slots=True)
class PeerState:
    name: str
    media: MediaInfo | None = None
    ready: bool = False
    ping_ms: float = 0.0
    clock_offset: float = 0.0
    drift_ms: int = 0
    last_correction: float = 0.0


@dataclass(slots=True)
class SessionState:
    session_id: str = ""
    role: Role = Role.NONE
    connected: bool = False
    local_ready: bool = False
    remote_ready: bool = False
    media_match: bool = False
    media_force_match: bool = False
    ping_ms: float = 0.0
    drift_ms: int = 0


@dataclass(slots=True)
class SeekState:
    command_id: int
    position_ms: int
    media_fingerprint: str
    resume: bool
    waiting: set[str] = field(default_factory=set)
    local_ready: bool = False
    stable_samples: int = 0
    failed: bool = False
