import time

from romarr.scheduler import BACKOFF_HOURS, Job, Scheduler, next_search_due


def test_a_disabled_job_is_never_due():
    s = Scheduler()
    job = s.add("X", "x", lambda: 0, lambda: "ran")
    assert not s._due(job)
    job2 = s.add("Y", "y", lambda: -5, lambda: "ran")
    assert not s._due(job2)


def test_a_job_with_an_interval_is_due_immediately_then_waits():
    s = Scheduler()
    ran = []
    job = s.add("X", "x", lambda: 3600, lambda: ran.append(1) or "ok")
    assert s._due(job), "never-run job with a live interval is due"
    s._run(job)
    assert ran == [1]
    assert not s._due(job), "just-run job must wait out its interval"


def test_a_crashing_job_is_recorded_not_raised():
    s = Scheduler()

    def boom():
        raise RuntimeError("indexer on fire")

    job = s.add("X", "x", lambda: 60, boom)
    s._run(job)  # must not raise
    assert "indexer on fire" in job.last_error
    assert job.last_result == ""
    # And it reschedules: the failure consumed this slot, not the job.
    assert not s._due(job)


def test_a_broken_interval_reader_disables_rather_than_crashes():
    s = Scheduler()

    def broken():
        raise KeyError("settings gone")

    job = s.add("X", "x", broken, lambda: "ok")
    assert not s._due(job)
    assert s.describe(job)["enabled"] is False


def test_run_now_ignores_unknown_names():
    s = Scheduler()
    assert s.run_now("Nothing") is None


def test_run_now_runs_and_reports():
    s = Scheduler()
    s.add("X", "the x job", lambda: 0, lambda: "42 things")
    out = s.run_now("X")
    assert out["last_result"] == "42 things"
    assert out["last_error"] == ""
    # run_now works even on a disabled job: pressing the button IS consent.
    assert out["enabled"] is False


def test_status_lists_every_job():
    s = Scheduler()
    s.add("A", "a", lambda: 60, lambda: "")
    s.add("B", "b", lambda: 0, lambda: "")
    names = [j["name"] for j in s.status()]
    assert names == ["A", "B"]


# -- backoff -----------------------------------------------------------------

def test_a_never_searched_item_is_always_due():
    assert next_search_due(0, "")
    assert next_search_due(99, "")


def test_a_just_searched_item_waits_out_its_ladder():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    assert next_search_due(0, now), "first rung of the ladder is zero hours"
    assert not next_search_due(1, now)
    assert not next_search_due(50, now), "attempts past the ladder use the cap"


def test_an_item_past_its_backoff_is_due_again():
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(timespec="seconds")
    assert next_search_due(1, old), "4h rung, 5h waited"
    assert not next_search_due(2, old), "8h rung, only 5h waited"


def test_garbage_timestamps_fail_open():
    # A corrupt timestamp must not freeze an item out of searching forever.
    assert next_search_due(3, "not-a-date")


def test_the_ladder_is_monotonic():
    assert list(BACKOFF_HOURS) == sorted(BACKOFF_HOURS)
