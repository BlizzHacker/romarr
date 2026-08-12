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


def test_a_never_run_job_is_due_on_a_freshly_booted_host(monkeypatch):
    """The bug this pins, and why it only ever failed in CI.

    `_last_started` defaulted to 0.0 and `time.monotonic()` counts from boot
    on Linux -- so 0.0 means "when this machine started", not "long ago". On
    a host up for 60 seconds, `monotonic() - 0.0` is 60, and every job whose
    interval exceeded the uptime looked not due. A daily job did not run
    until the host had been up a day. On a workstation up for weeks the same
    code looked correct, which is why every Docker build failed while the
    suite passed locally.
    """
    import romarr.scheduler as sched

    # A host that booted one minute ago.
    monkeypatch.setattr(sched.time, "monotonic", lambda: 60.0)

    s = Scheduler()
    daily = s.add("Daily", "d", lambda: 86400, lambda: "ok")
    assert s._due(daily), "a never-run job must not wait for uptime"

    # And once it has run, the interval applies normally.
    s._run(daily)
    assert not s._due(daily)


def test_uptime_shorter_than_the_interval_does_not_block_the_first_run(monkeypatch):
    import romarr.scheduler as sched

    monkeypatch.setattr(sched.time, "monotonic", lambda: 5.0)
    s = Scheduler()
    for interval in (60, 3600, 86400):
        job = s.add(f"J{interval}", "j", lambda i=interval: i, lambda: "ok")
        assert s._due(job), f"interval {interval} blocked on a 5s uptime"
