'''Timers to manage lease rebinding, renewal & expiration.'''

import asyncio
import dataclasses
from logging import getLogger
from typing import Awaitable, Callable, Optional

from pyroute2.dhcp.leases import Lease

LOG = getLogger(__name__)


@dataclasses.dataclass
class Timers:
    '''Manage callbacks associated with DHCP leases.'''

    renewal: Optional[asyncio.TimerHandle] = None
    rebinding: Optional[asyncio.TimerHandle] = None
    expiration: Optional[asyncio.TimerHandle] = None

    def cancel(self):
        '''Cancel all current timers.'''
        for timer_name in ('renewal', 'rebinding', 'expiration'):
            self._reset_timer(timer_name)

    def _reset_timer(self, timer_name: str):
        '''Cancel a timer and set it to None.'''
        if timer := getattr(self, timer_name):
            if not timer.cancelled():
                # FIXME: how do we know a timer wasn't cancelled ?
                # this causes spurious logs
                LOG.debug('Canceling %s timer', timer_name)
                timer.cancel()
            setattr(self, timer_name, None)

    def arm(self, lease: Lease, **callbacks: Callable[[], Awaitable[None]]):
        '''Reset & arm timers from a `Lease`.

        `callbacks` must be async callables with no arguments
        that will be called when the associated timer expires.
        '''
        self.cancel()
        loop = asyncio.get_running_loop()

        for timer_name, async_callback in callbacks.items():
            self._reset_timer(timer_name)
            lease_time = getattr(lease, f'{timer_name}_in')
            if not lease_time:
                LOG.debug('Lease does not set a %s time', timer_name)
                continue
            if lease_time < 0.0:
                LOG.debug('Lease %s is in the past', timer_name)
                continue
            LOG.info('Scheduling lease %s in %.2fs', timer_name, lease_time)
            # Since call_later doesn't support async callbacks, we wrap the
            # callback in a lambda that will schedule it when it's time
            timer = loop.call_later(
                lease_time,
                # since lambdas are evaluated when they're run, we have to
                # bind variables as argument defaults or they'll have the
                # value from the last loop iteration
                lambda cb=async_callback, name=lease_time: asyncio.create_task(
                    cb(), name=f"{name} timer callback"
                ),
            )
            setattr(self, timer_name, timer)
