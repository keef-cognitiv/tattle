import asyncio
import ipaddress
import socket

import netifaces

from . import logging

__all__ = [
    'TCPListener',
    'UDPConnection',
    'UDPListener',
    'parse_address',
    'default_ip_address',
]

LOG = logging.get_logger(__name__)


class AbstractListener:
    def __init__(self, listen_address, listen_port, callback, loop=None):
        self._listen_address = listen_address
        self._listen_port = listen_port
        self._callback = callback
        self._loop = loop or asyncio.get_event_loop()

    def _run_callback(self, *args, **kwargs):
        try:
            if self._callback is not None:
                res = self._callback(*args, **kwargs)
                if asyncio.coroutines.iscoroutine(res):
                    self._loop.create_task(res)
        except:
            LOG.exception("Error running callback")

    @property
    def local_address(self):
        raise NotImplementedError

    @property
    def local_port(self):
        raise NotImplementedError

    async def start(self):
        raise NotImplementedError

    async def stop(self):
        raise NotImplementedError


class TCPListener(AbstractListener):
    """
    The TCPListener listens for messages over TCP
    """

    def __init__(self, listen_address, listen_port, callback, loop=None):
        super().__init__(listen_address, listen_port, callback, loop)

    @property
    def local_address(self):
        for s in self._server.sockets:
            if s.family == socket.AF_INET:
                return s.getsockname()[0]

    @property
    def local_port(self):
        for s in self._server.sockets:
            if s.family == socket.AF_INET:
                return s.getsockname()[1]

    async def start(self):
        self._server = await asyncio.start_server(self._handle_connection,
                                                  self._listen_address,
                                                  self._listen_port)

    async def stop(self):
        self._server.close()

    async def _handle_connection(self, client_reader, client_writer):
        # get client address from the underlying transport
        addr = client_reader._transport.get_extra_info('peername')

        LOG.trace("Handing incoming TCP connection from: %s", addr)

        self._run_callback(client_reader, client_writer, addr)

        LOG.trace("Finished handling TCP connection from: %s", addr)


class UDPConnection:
    def __init__(self, event_loop=None):
        self._event_loop = event_loop or asyncio.get_event_loop()
        self._socket = self._create_socket()

    def _create_socket(self):
        udpsock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udpsock.setblocking(False)
        return udpsock

    @property
    def local_address(self):
        return self._socket.getsockname()[0]

    @property
    def local_port(self):
        return self._socket.getsockname()[1]

    def connect(self, addr):
        """
        Connect to a given address (for use with send)
        :param addr:
        :return:
        """
        future = self._event_loop.sock_connect(self._socket, addr)
        LOG.trace("UDP connection bind: %s", addr)
        return future

    def bind(self, addr):
        """
        Bind the connection to given port and address
        :param addr:
        :return:
        """
        self._socket.bind(addr)
        LOG.trace("UDP connection bind: %s", addr)

    def close(self):
        """
        Close connection
        :return: None
        """
        self._socket.close()
        LOG.trace("UDP connection closed")

    def send(self, data):
        """
        Send data
        :param data:
        :return: None on success, otherwise exception is raised
        """
        return self._event_loop.sock_sendall(self._socket, data)

    def sendto(self, data, addr):
        """
        Send data
        :param addr:
        :param data:
        :return: None on success, otherwise exception is raised
        """
        fut = self._event_loop.create_future()
        if data:
            self._sendto(data, addr, fut, False)
        else:
            fut.set_result(None)
        return fut

    def _sendto(self, data, addr, fut, registered):
        fd = self._socket.fileno()

        # Remove the writer if already registered
        if registered:
            self._event_loop.remove_writer(fd)

        if fut.cancelled():
            return

        try:
            n = self._socket.sendto(data, addr)
        except (BlockingIOError, InterruptedError):
            # if we're going to block, we'll add a writer (see below)
            n = 0
        except Exception as exc:
            fut.set_exception(exc)
            return

        # get data left to be send
        if n == len(data):
            fut.set_result(None)
        else:
            if n:
                data = data[n:]
            self._event_loop.add_writer(fd, self._sendto, data, addr, fut, True)

    def recv(self, read_bytes):
        """
        Receive data
        :param read_bytes:
        :return: Bytes read
        """
        return self._event_loop.sock_recv(self._socket, read_bytes)

    def recvfrom(self, read_bytes):
        """
        Receive data
        :param read_bytes:
        :return: Bytes read
        """
        fut = self._event_loop.create_future()
        self._recvfrom(read_bytes, fut, False)
        return fut

    def _recvfrom(self, read_bytes, fut, registered):

        fd = self._socket.fileno()

        # Remove the reader if already registered
        if registered:
            self._event_loop.remove_reader(fd)

        if fut.cancelled():
            return

        try:
            data, addr = self._socket.recvfrom(read_bytes)
        except (BlockingIOError, InterruptedError):
            # if we're going to block, add a reader
            self._event_loop.add_reader(fd, self._recvfrom, read_bytes, fut, True)
        except Exception as exc:
            fut.set_exception(exc)
        else:
            fut.set_result((data, addr))


class UDPListener(AbstractListener):
    """
    The UDPListener listens for messages via UDP
    """

    def __init__(self, listen_address, listen_port, callback, loop=None, read_size=1024):
        super().__init__(listen_address, listen_port, callback, loop)
        self._read_size = read_size
        self._connection = UDPConnection(self._loop)
        self._connection.bind((listen_address, listen_port))
        self._task = None

    @property
    def local_address(self):
        return self._connection.local_address

    @property
    def local_port(self):
        return self._connection.local_port

    def sendto(self, data, address, port):
        """
        Send data
        :param data:
        :param address:
        :param port:
        :return:
        """
        return self._connection.sendto(data, (address, port))

    async def start(self):
        """
        Start the listener
        :return:
        """
        self._read_data()

    async def stop(self):
        """
        Stop the listener
        :return:
        """
        self._task.cancel()
        self._connection.close()

    def _read_data(self):
        # wait for data
        self._task = asyncio.ensure_future(self._connection.recvfrom(self._read_size), loop=self._loop)
        self._task.add_done_callback(self._handle_data)

    def _handle_data(self, future):
        """
        _handle_data is called when data has been read
        """
        try:

            # check if future was cancelled
            if future.cancelled():
                return

            # handle future error
            error = future.exception()
            if error is not None:
                LOG.error(error, exc_info=future.exception())
                return

            # get future result
            data, addr = future.result()

            # run callback
            self._run_callback(data, addr)

        except:
            LOG.exception("Error handling data")

        self._read_data()


def parse_address(address):
    if ':' in address:
        host, _, port = address.partition(':')
        return host, int(port)
    return address, None


def default_ip_address():
    """
    Return the IP address of the first interface with a real IP addresses
    """
    devices = netifaces.interfaces()
    for device in devices:
        addresses = netifaces.ifaddresses(device)
        if netifaces.AF_INET in addresses:
            for addr in addresses[netifaces.AF_INET]:
                ip_addr = ipaddress.ip_address(addr['addr'])
                if not ip_addr.is_loopback and (ip_addr.is_global or ip_addr.is_private):
                    return str(ip_addr)
    return None
