"""Fix proposals, kept in memory only. A pod restart drops them, by design."""

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Proposal:
    id: str
    diff: str
    base_sha: str
    created_at: float
    service: str
    summary: str
    question: str
    cause: str
    evidence: list[dict] = field(default_factory=list)


class ProposalStore:
    def __init__(self, ttl_seconds: float = 24 * 3600, clock: Callable[[], float] = time.time) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._items: dict[str, Proposal] = {}
        self._lock = threading.Lock()

    def add(
        self, *, diff: str, base_sha: str, service: str, summary: str, question: str, cause: str, evidence: list[dict]
    ) -> Proposal:
        proposal = Proposal(
            id=uuid.uuid4().hex,
            diff=diff,
            base_sha=base_sha,
            created_at=self._clock(),
            service=service,
            summary=summary,
            question=question,
            cause=cause,
            evidence=list(evidence),
        )
        with self._lock:
            self._purge()
            self._items[proposal.id] = proposal
        return proposal

    def get(self, proposal_id: str) -> Proposal | None:
        with self._lock:
            self._purge()
            return self._items.get(proposal_id)

    def remove(self, proposal_id: str) -> None:
        with self._lock:
            self._items.pop(proposal_id, None)

    def _purge(self) -> None:
        now = self._clock()
        for pid in [pid for pid, p in self._items.items() if now - p.created_at >= self._ttl]:
            del self._items[pid]
