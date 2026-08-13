"""In-memory registry of SAP systems, persisted to a JSON file."""
import json
import os
from dataclasses import dataclass, asdict


@dataclass
class System:
    name: str
    url: str
    client: str
    language: str
    auth: str  # "basic" | "cookie"
    username: str | None
    password: str | None
    cookie_file: str | None
    cookie_string: str | None
    allow_write: bool = False
    write_packages: list[str] | None = None
    write_objects: list[str] | None = None
    # Debugging is gated separately from writing: run_class executes arbitrary
    # ABAP on the tenant, which outlives any single source change.
    allow_debug: bool = False
    # HTTP ceiling while a debug session is live. Code stopped at a breakpoint
    # holds the request that started it, so this is really "how long may I sit
    # at a breakpoint before the run is cut loose".
    debug_timeout: int = 600
    debug_listen_seconds: int = 120

    @classmethod
    def from_dict(cls, name: str, d: dict) -> "System":
        return cls(
            name=name,
            url=d["url"],
            client=d.get("client", "001"),
            language=d.get("language", "EN"),
            auth=d.get("auth", "basic"),
            username=d.get("username"),
            password=d.get("password"),
            cookie_file=d.get("cookie_file"),
            cookie_string=d.get("cookie_string"),
            allow_write=d.get("allow_write", False),
            write_packages=d.get("write_packages"),
            write_objects=d.get("write_objects"),
            allow_debug=d.get("allow_debug", False),
            debug_timeout=int(d.get("debug_timeout", 600)),
            debug_listen_seconds=int(d.get("debug_listen_seconds", 120)),
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("name")
        # Only persist flags that were actually turned on / tuned, so a config
        # written back keeps looking like the one the user wrote by hand.
        for field, default in (("allow_write", False), ("allow_debug", False),
                               ("debug_timeout", 600),
                               ("debug_listen_seconds", 120)):
            if d.get(field) == default:
                d.pop(field, None)
        return {k: v for k, v in d.items() if v is not None}


class SystemRegistry:
    def __init__(self, path: str):
        self._path = path
        self._systems: dict[str, System] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        with open(self._path, encoding="utf-8") as f:
            raw = json.load(f)
        self._systems = {name: System.from_dict(name, d)
                         for name, d in raw.items()}

    def _save(self) -> None:
        raw = {s.name: s.to_dict() for s in self._systems.values()}
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2, ensure_ascii=False)

    def list(self) -> list[System]:
        return list(self._systems.values())

    def get(self, name: str) -> System:
        return self._systems[name]

    def upsert(self, system: System) -> None:
        self._systems[system.name] = system
        self._save()

    def delete(self, name: str) -> None:
        self._systems.pop(name, None)
        self._save()
