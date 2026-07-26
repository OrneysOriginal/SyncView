from dataclasses import dataclass

from src.session.role import Role


@dataclass(slots=True)
class SessionState:
    role: Role = Role.NONE
    connected: bool = False
    local_ready: bool = False
    remote_ready: bool = False
    media_match: bool = False
    media_force_match: bool = False
    ping_ms: float = 0.0
    drift_ms: int = 0
