from app.conversation import ConversationStore, PendingClarification


def test_pending_clarification_is_combined_once_and_sessions_are_isolated():
    store = ConversationStore(ttl_seconds=60)
    first = store.session_id(None)
    second = store.session_id(None)
    store.require_clarification(first, "查询哪个县装机最多？")

    assert store.resolve(second, "只看风电") == "只看风电"
    assert store.resolve(first, "只看风电") == "原问题：查询哪个县装机最多？\n用户补充：只看风电"
    assert store.resolve(first, "新问题") == "新问题"


def test_expired_context_and_length_budget():
    store = ConversationStore(ttl_seconds=60)
    session_id = store.session_id(None)
    store._pending[session_id] = PendingClarification("旧问题", expires_at=0)
    assert store.resolve(session_id, "新问题") == "新问题"
    store.require_clarification(session_id, "问" * 2000)
    assert len(store.resolve(session_id, "答" * 2000)) <= 2000


def test_health_summary_makes_single_replica_boundary_explicit():
    store = ConversationStore(ttl_seconds=12, max_sessions=7)

    assert store.health_summary() == {
        "backend": "memory",
        "multi_replica_supported": False,
        "ttl_seconds": 12,
        "max_sessions": 7,
        "pending_sessions": 0,
    }
