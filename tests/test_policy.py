from healer.policy import (
    ALERT_PLAYBOOKS,
    Action,
    ActionBudget,
    Cooldown,
    decide_crashloop_action,
    playbook_for_alert,
    replicas_to_restore,
)


def crashloop(**overrides):
    args = dict(
        restart_count=3, threshold=2, has_previous_revision=True,
        current_revision_age_seconds=60, rollback_window_seconds=900, ready_pods_on_current_revision=0,
    )
    args.update(overrides)
    return decide_crashloop_action(**args)


def test_every_alert_rule_with_a_playbook_maps_to_an_action():
    for name in ("HighErrorRate", "HighMemoryUsage", "HighLatency", "ServiceDown", "PodCrashLooping"):
        assert playbook_for_alert(name) is ALERT_PLAYBOOKS[name]
    assert playbook_for_alert("PodRestartBurst") is None


def test_fresh_release_that_never_became_ready_is_rolled_back():
    assert crashloop() is Action.ROLLBACK


def test_crashloop_below_threshold_is_ignored():
    assert crashloop(restart_count=1) is None


def test_old_revision_crashloop_recycles_pod_instead_of_rolling_back():
    assert crashloop(current_revision_age_seconds=5000) is Action.RESTART_POD


def test_no_previous_revision_means_restart():
    assert crashloop(has_previous_revision=False) is Action.RESTART_POD


def test_healthy_sibling_pods_mean_the_release_is_fine():
    assert crashloop(ready_pods_on_current_revision=1) is Action.RESTART_POD


def test_replicas_to_restore():
    assert replicas_to_restore(0, 2) == 2
    assert replicas_to_restore(1, 2) == 2
    assert replicas_to_restore(2, 2) is None
    assert replicas_to_restore(5, 2) is None
    assert replicas_to_restore(0, 0) is None


def test_cooldown_blocks_until_period_passes():
    cooldown = Cooldown(30)
    assert cooldown.ready("pod-a", 100)
    cooldown.mark("pod-a", 100)
    assert not cooldown.ready("pod-a", 110)
    assert cooldown.remaining("pod-a", 110) == 20
    assert cooldown.ready("pod-b", 110)
    assert cooldown.ready("pod-a", 130)
    assert cooldown.remaining("never", 0) == 0


def test_budget_opens_circuit_then_recovers_as_window_slides():
    budget = ActionBudget(max_actions=2, window_seconds=60)
    budget.record("orders", 0)
    budget.record("orders", 10)
    assert not budget.allow("orders", 20)
    assert budget.used("orders", 20) == 2
    assert budget.allow("payments", 20)
    assert budget.allow("orders", 61)  # first action aged out
    assert budget.used("orders", 61) == 1


def test_rollback_prefers_last_known_good_revision():
    from healer.policy import choose_rollback_target

    # rev 2 and 3 are both broken releases (0 ready); rev 1 is still serving.
    assert choose_rollback_target([(1, 2), (2, 0), (3, 0)]) == 1
    # Normal case: the previous revision is the healthy one.
    assert choose_rollback_target([(4, 2), (5, 0)]) == 4
    # Nothing healthy anywhere: fall back to the previous revision.
    assert choose_rollback_target([(1, 0), (2, 0), (3, 0)]) == 2
    assert choose_rollback_target([(1, 0)]) is None
