'''
IPv4 DHCP socket
================

'''

import asyncio
import logging
import socket
from typing import Optional

from pyroute2.compat import ETHERTYPE_IP
from pyroute2.dhcp.dhcp4msg import dhcp4msg
from pyroute2.dhcp.messages import ReceivedDHCPMessage, SentDHCPMessage
from pyroute2.ext.bpf import BPF
from pyroute2.ext.rawsocket import AsyncRawSocket
from pyroute2.protocols import ethmsg, ip4msg, udp4_pseudo_header, udpmsg

LOG = logging.getLogger(__name__)


UDP_HEADER_SIZE = 8
IPV4_HEADER_SIZE = 20
SKF_AD_OFF = -0x1000
SKF_AD_VLAN_TAG_PRESENT = 48


def listen_udp_port(port: int = 68) -> list[list[int]]:
    '''BPF filter that matches Ethernet + IPv4 + UDP on the given port.

    Packets tagged on a vlan are also dropped, see
    https://lore.kernel.org/netdev/51FB6A9D.2050002@redhat.com/T/
    '''
    bpf_code = [
        # Load vlan presence indicator
        [BPF.LD + BPF.B + BPF.ABS, 0, 0, SKF_AD_OFF + SKF_AD_VLAN_TAG_PRESENT],
        # bail out immediately if there is one and we don't want it
        [BPF.JMP + BPF.JEQ + BPF.K, 0, 10, 0],
        # Load eth type at offset 12 and check it's IPv4
        [BPF.LD + BPF.H + BPF.ABS, 0, 0, 12],
        [BPF.JMP + BPF.JEQ + BPF.K, 0, 8, 0x0800],
        # Load IP proto at offset 23 and check it's UDP
        [BPF.LD + BPF.B + BPF.ABS, 0, 0, 23],
        [BPF.JMP + BPF.JEQ + BPF.K, 0, 6, socket.IPPROTO_UDP],
        # load frag offset at offset 20
        [BPF.LD + BPF.H + BPF.ABS, 0, 0, 20],
        # Check mask & drop fragmented packets
        [BPF.JMP + BPF.JSET + BPF.K, 4, 0, 8191],
        # load ip header length at offset 14
        [BPF.LDX + BPF.B + BPF.MSH, 0, 0, 14],
        # load udp dport from that offset + 16 and check it
        [BPF.LD + BPF.H + BPF.IND, 0, 0, 16],
        [BPF.JMP + BPF.JEQ + BPF.K, 0, 1, port],
        # allow packet
        [BPF.RET + BPF.K, 0, 0, 65535],
        # drop packet
        [BPF.RET + BPF.K, 0, 0, 0],
    ]
    return bpf_code


class AsyncDHCP4Socket(AsyncRawSocket):
    '''
    Parameters:

    * ifname -- interface name to work on

    This raw socket binds to an interface and installs BPF filter
    to get only its UDP port. It can be used in poll/select and
    provides also the context manager protocol, so can be used in
    `with` statements.
    '''

    def __init__(self, ifname, port: int = 68):
        AsyncRawSocket.__init__(self, ifname, listen_udp_port(port))
        self.port = port
        self._loop = asyncio.get_running_loop()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        # We define this as a property because it's easier to patch in tests
        return self._loop

    async def put(self, msg: SentDHCPMessage) -> SentDHCPMessage:
        '''
        Put DHCP message.

        Examples::

            sock.put(dhcp4msg({'op': BOOTREQUEST,
                               'chaddr': 'ff:11:22:33:44:55',
                               'options': {'message_type': DHCPREQUEST,
                                           'parameter_list': [1, 3, 6, 12, 15],
                                           'requested_ip': '172.16.101.2',
                                           'server_id': '172.16.101.1'}}))

        The method returns the SentDHCPMessage, so one can get from
        there the `xid` (transaction id) and other details.
        '''

        if msg.sport != self.port:
            raise ValueError(
                f"Client source port is set to {self.port}, "
                f"cannot send message from port {msg.sport}."
            )

        if not msg.eth_src:
            msg.eth_src = self.l2addr

        # DHCP layer
        dhcp = msg.dhcp

        # auto add src addr
        if dhcp['chaddr'] is None:
            dhcp['chaddr'] = msg.eth_src

        data = dhcp.encode().buf
        dhcp_payload_size = len(data)

        # UDP layer
        udp = udpmsg(
            {
                'sport': self.port,
                'dport': msg.dport,
                'len': UDP_HEADER_SIZE + dhcp_payload_size,
            }
        )
        # Pseudo UDP header, only for checksum purposes
        udph = udp4_pseudo_header(
            {
                'src': msg.ip_src,
                'dst': msg.ip_dst,
                'len': UDP_HEADER_SIZE + dhcp_payload_size,
            }
        )
        udp['csum'] = self.csum(udph.encode().buf + udp.encode().buf + data)
        udp.reset()

        # IPv4 layer
        ip4 = ip4msg(
            {
                'len': IPV4_HEADER_SIZE + UDP_HEADER_SIZE + dhcp_payload_size,
                'proto': socket.IPPROTO_UDP,
                'dst': msg.ip_dst,
                'src': msg.ip_src,
            }
        )
        ip4['csum'] = self.csum(ip4.encode().buf)
        ip4.reset()

        # MAC layer
        eth = ethmsg(
            {'dst': msg.eth_dst, 'src': msg.eth_src, 'type': ETHERTYPE_IP}
        )

        data = eth.encode().buf + ip4.encode().buf + udp.encode().buf + data
        await self.loop.sock_sendall(self, data)
        dhcp.reset()
        return msg

    async def get(self) -> ReceivedDHCPMessage:
        '''
        Get the next incoming packet from the socket and try
        to decode it as IPv4 DHCP. No analysis is done here,
        only MAC/IPv4/UDP headers are stripped out, and the
        rest is interpreted as DHCP.

        Packets that cannot be decoded are logged & discarded.
        '''
        msg: Optional[ReceivedDHCPMessage] = None
        while not msg:
            raw = await self.loop.sock_recv(self, 4096)
            try:
                msg = self._decode_msg(raw)
            except ValueError as err:
                LOG.error('%s', err)
        return msg

    @classmethod
    def _decode_msg(cls, data: bytes) -> ReceivedDHCPMessage:
        '''Decode and return an IPv4 DHCP packet from bytes.'''
        eth = ethmsg(buf=data).decode()
        ip4 = ip4msg(buf=data, offset=eth.offset).decode()
        udp = udpmsg(buf=data, offset=ip4.offset).decode()
        dhcp = dhcp4msg(buf=data, offset=udp.offset).decode()
        return ReceivedDHCPMessage(
            dhcp=dhcp,
            eth_src=eth['src'],
            eth_dst=eth['dst'],
            ip_src=ip4['src'],
            ip_dst=ip4['dst'],
            sport=udp['sport'],
            dport=udp['dport'],
        )
