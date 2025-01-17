import asyncio
from dataclasses import dataclass
from shutil import which
from typing import AsyncGenerator, ClassVar, Optional

import pytest
import pytest_asyncio
from fixtures.interfaces import DHCPRangeConfig

from . import DHCPServerConfig, DHCPServerFixture, run_fixture_as_main


@dataclass
class DnsmasqConfig(DHCPServerConfig):
    '''Options for the dnsmasq server.'''

    def __iter__(self):
        opts = [
            f'--interface={self.interface}',
            f'--dhcp-range={self.range.start},'
            f'{self.range.end},{self.lease_time}',
            f'--dhcp-lease-max={self.max_leases}',
        ]
        if router := self.range.router:
            opts.append(f"--dhcp-option=option:router,{router}")
        return iter(opts)


class DnsmasqFixture(DHCPServerFixture[DnsmasqConfig]):
    '''Runs the dnsmasq server as an async context manager.'''

    BINARY_PATH: ClassVar[Optional[str]] = which('dnsmasq')

    def _get_base_cmdline_options(self) -> tuple[str]:
        '''The base commandline options for dnsmasq.'''
        return (
            '--keep-in-foreground',  # self explanatory
            '--no-resolv',  # don't mess w/ resolv.conf
            '--log-facility=-',  # log to stdout
            '--no-hosts',  # don't read /etc/hosts
            '--bind-interfaces',  # don't bind on wildcard
            '--no-ping',  # don't ping to check if ips are attributed
        )

    def get_cmdline_options(self) -> tuple[str]:
        '''All commandline options passed to dnsmasq.'''
        return (*self._get_base_cmdline_options(), *self.config)


@pytest.fixture
def dnsmasq_config(
    veth_pair: tuple[str, str], dhcp_range: DHCPRangeConfig
) -> DnsmasqConfig:
    '''dnsmasq options useful for test purposes.'''
    return DnsmasqConfig(range=dhcp_range, interface=veth_pair[0])


@pytest_asyncio.fixture
async def dnsmasq(
    dnsmasq_config: DnsmasqConfig,
) -> AsyncGenerator[DnsmasqFixture, None]:
    '''A dnsmasq instance running for the duration of the test.'''
    async with DnsmasqFixture(config=dnsmasq_config) as dnsf:
        yield dnsf


if __name__ == '__main__':
    asyncio.run(run_fixture_as_main(DnsmasqFixture))
