"""Cooling configuration precedence regression tests."""
from core.scheduler import Scheduler


def test_canonical_scheduler_cooling_overrides_legacy_top_level(tmp_path):
    cfg = {
        "cooling": {
            "after_success_minutes": 30,
            "after_failure_minutes": 30,
        },
        "scheduler": {
            "cooling": {
                "after_success_minutes": 5,
                "after_failure_minutes": 5,
                "after_platform_failure_minutes": 5,
            },
        },
    }
    scheduler = Scheduler(cfg, str(tmp_path))

    success = scheduler.cooldown_breakdown("success")
    failure = scheduler.cooldown_breakdown("failure", failure_streak=1)

    assert success["configured_after_success_minutes"] == 5.0
    assert success["clamped_minutes"] == 5.0
    assert failure["configured_after_failure_minutes"] == 5.0
    assert failure["clamped_minutes"] == 5.0
