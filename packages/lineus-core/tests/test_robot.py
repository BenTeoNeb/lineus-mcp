from lineus_core import robot as rb


def test_a_finished_drawing_lets_go_of_the_arm(monkeypatch):
    closed = []
    monkeypatch.setattr(rb.robot, "close", lambda: closed.append(True))
    job = rb.Job(id="t", strokes=[[(30, 20), (35, 20)]], total=2)
    rb.run_job(job)
    assert job.state == "done" and closed, "the arm takes one client at a time: release it"


def test_status_does_not_pull_the_arm_away_from_a_running_drawing(monkeypatch):
    closed = []
    monkeypatch.setattr(rb.robot, "close", lambda: closed.append(True))
    with rb.job_lock:
        rb.release_if_idle()
    assert not closed
    rb.release_if_idle()
    assert closed
