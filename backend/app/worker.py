"""RQ worker variants required by supported development platforms."""
from rq.timeouts import TimerDeathPenalty
from rq.worker import SimpleWorker


class WindowsWorker(SimpleWorker):
    """In-process worker using timer timeouts instead of fork/SIGALRM."""

    death_penalty_class = TimerDeathPenalty
