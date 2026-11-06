from k8s_agent.proposals import ProposalStore


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _add(store, **kw):
    args = {
        "diff": "d",
        "base_sha": "a" * 40,
        "service": "x",
        "summary": "s",
        "question": "q",
        "cause": "c",
        "evidence": [{"source": "events", "detail": "e"}],
    }
    args.update(kw)
    return store.add(**args)


def test_add_and_get():
    store = ProposalStore(clock=Clock())
    p = _add(store)
    assert len(p.id) == 32
    assert store.get(p.id) == p
    assert store.get("nope") is None


def test_proposals_expire_after_24_hours():
    clock = Clock()
    store = ProposalStore(ttl_seconds=24 * 3600, clock=clock)
    p = _add(store)
    clock.now += 24 * 3600 - 1
    assert store.get(p.id) is not None
    clock.now += 1
    assert store.get(p.id) is None


def test_a_new_store_knows_nothing_like_after_a_restart():
    p = _add(ProposalStore(clock=Clock()))
    assert ProposalStore(clock=Clock()).get(p.id) is None


def test_remove():
    store = ProposalStore(clock=Clock())
    p = _add(store)
    store.remove(p.id)
    assert store.get(p.id) is None
