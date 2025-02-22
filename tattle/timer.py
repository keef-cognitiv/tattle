import asyncio
import time

from . import logging

__all__ = [
    'Timer'
]

LOG = logging.get_logger(__name__)


class Timer(object):
    def __init__(self, callback, time, loop=None):
        self.func = callback
        self.time = time
        self._loop = loop or asyncio.get_event_loop()
        self._handle = None
        self._started = None

    def start(self):
        assert self._handle is None
        self._started = time.time()
        self._handle = self._loop.call_later(self.time, self._run)

    def reset(self, time):
        self.stop()
        self.time = time
        self.start()

    def remaining(self):
        if self._started is None:
            return self.time

        return max(0, self.time - (time.time() - self._started))

    def stop(self):
        if self._handle is not None:
            self._handle.cancel()
        self._handle = None

    def _run(self):
        LOG.trace("Running timer callback: %s", self.func)
        res = self.func()
        if asyncio.coroutines.iscoroutine(res):
            self._loop.create_task(res)

    def __del__(self):
        self.stop()
