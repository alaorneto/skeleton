"""The job deadline interrupts a handler past its timeout_s (#297)."""

import threading
import time

import pytest

from skeleton.jobs.worker import JobTimeout, _deadline


def test_deadline_is_a_no_op_off_the_main_thread():
    errors = []

    def run():
        try:
            with _deadline(1):
                time.sleep(0.1)
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    assert errors == []


@pytest.mark.posix
def test_deadline_interrupts_and_restores_the_previous_handler():
    import signal

    previous = signal.getsignal(signal.SIGALRM)
    started = time.monotonic()
    with pytest.raises(JobTimeout), _deadline(1):
        time.sleep(5)
    assert time.monotonic() - started < 3
    assert signal.getsignal(signal.SIGALRM) == previous


def test_smtp_has_a_default_timeout():
    from skeleton.conf.mailers import mailer_from_url

    assert mailer_from_url("smtp://mail.example")["OPTIONS"]["timeout"] == 30
