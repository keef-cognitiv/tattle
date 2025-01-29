import asyncio
import collections
import io
import math
import struct
import time

from . import config
from . import event
from . import logging
from . import messages
from . import network
from . import schedule
from . import state
from . import sequence
from . import queue
from . import utilities

__all__ = [
    'Cluster'
]

LOG = logging.get_logger(__name__)

ProbeStatus = collections.namedtuple('ProbeStatus', ['future'])


def _calculate_transmit_limit(n, m):
    scale = math.ceil(math.log10(n + 1))
    return scale * m


class Cluster(object):
    def __init__(self, config: config.Configuration, loop=None):
        """
        Create a new instance of the Cluster class
        """
        self.config = config

        self._metadata = {}

        self._user_message_callback = None

        self._loop = loop or asyncio.get_event_loop()

        self._udp_listener = self._init_listener_udp()
        self._tcp_listener = self._init_listener_tcp()

        self._events = self._init_events()

        self._queue = self._init_queue()

        self._nodes = self._init_nodes()

        self._init_probe()

        self._init_sync()

    def _init_listener_udp(self):
        udp_listener = network.UDPListener(self.config.bind_address,
                                           self.config.bind_port,
                                           self._handle_udp_data,
                                           loop=self._loop)
        return udp_listener

    def _init_listener_tcp(self):
        tcp_listener = network.TCPListener(self.config.bind_address,
                                           self.config.bind_port,
                                           self._handle_tcp_connection,
                                           loop=self._loop)
        return tcp_listener

    def _init_queue(self):
        return queue.BroadcastQueue()

    def _init_events(self):
        return event.EventManager()

    def _init_nodes(self):
        return state.NodeManager(self.config, self._queue, self._events, loop=self._loop)

    def _init_probe(self):
        self._probe_schedule = schedule.ScheduledCallback(self._do_probe, self.config.probe_interval, loop=self._loop)
        self._probe_index = 0
        self._probe_status = dict()
        self._probe_seq = sequence.Sequence()

    def _init_sync(self):
        self._sync_schedule = schedule.ScheduledCallback(self._do_sync, self.config.sync_interval, loop=self._loop)

    async def start(self):
        """
        Start the cluster on this node.

        :return: None
        """
        await self._udp_listener.start()
        LOG.debug("Started UDPListener. Listening on udp %s:%d",
                  self._udp_listener.local_address,
                  self._udp_listener.local_port)

        await self._tcp_listener.start()
        LOG.debug("Started TCPListener. Listening on tcp %s:%d",
                  self._tcp_listener.local_address,
                  self._tcp_listener.local_port)

        # setup local node
        await self._nodes.set_local_node(self.local_node_name,
                                         self.local_node_address,
                                         self.local_node_port,
                                         self.local_metadata)

        # schedule callbacks
        await self._probe_schedule.start()
        await self._sync_schedule.start()

        LOG.info("Node started")

    @property
    def local_metadata(self):
        if self._nodes.local_node is not None:
            return self._nodes.local_node.metadata
        return self._metadata

    async def stop(self):
        """
        Shutdown the cluster on this node. This will cause this node to appear dead to other nodes.

        :return: None
        """
        await self._probe_schedule.stop()
        await self._sync_schedule.stop()
        await self._tcp_listener.stop()
        await self._udp_listener.stop()

        LOG.info("Node stopped")

    async def join(self, *node_address):
        """
        Join a cluster.

        :return: None
        """

        # gather list of nodes to sync
        LOG.trace("Attempting to join nodes: %s", node_address)

        # sync nodes
        tasks = []
        for node_host, node_port in node_address:
            tasks.append(asyncio.ensure_future(self._sync_host(node_host, node_port)))

        # wait for syncs to complete
        results = await asyncio.gather(*tasks, return_exceptions=True)
        successful_nodes, failed_nodes = utilities.partition(lambda r: r is True, results)
        LOG.debug("Successfully synced %d nodes (%d failed)", len(successful_nodes), len(failed_nodes))

    async def leave(self):
        """
        Leave the cluster.

        :return: None
        """
        await self._nodes.leave_local_node()

    async def sync(self, node):
        """
        Sync this node with another node.

        :param node: node name
        :return:
        """
        target_node = self._nodes.get(node)
        await self._sync_node(target_node)

    async def ping(self, node, indirect=False):
        """
        Ping a node.

        :param node: node name
        :param indirect:
        :return: None
        """
        target_node = self._nodes.get(node)
        if indirect:
            await self._probe_node_indirect(target_node, self.config.probe_indirect_nodes)
        else:
            await self._probe_node(target_node)

    async def send(self, node: state.Node | str, data, reliable=False):
        """
        Send a user message to a node

        :param node: node name
        :param data: message data
        :param reliable: reliable delivery
        """
        target_node = self._nodes.get(node) if not isinstance(node, state.Node) else self._nodes.get(node.name)
        reliable = reliable or len(data) > 65000

        if reliable:
            await self._ensure_connected(target_node)
            await self._send_tcp_message(messages.UserMessage(data=data, sender=self.local_node_name),
                                         target_node.write_stream)
        else:
            await self._send_udp_message(target_node.host, target_node.port,
                                         messages.UserMessage(data=data, sender=self.local_node_name))

    def on_user_message(self, callback):
        self._user_message_callback = callback

    @property
    def members(self):
        """
        Return the nodes in the cluster
        """
        return sorted(self._nodes, key=lambda n: n.name)

    def subscribe(self, event, handler):
        """
        Add a handler to an event

        :param event:
        :param handler:
        :return: None
        """
        return self._events.on(event, handler)

    def unsubscribe(self, event, handler):
        """
        Remove an handler from an event

        :param event: event
        :param handler: handler function
        :return: None
        """
        return self._events.off(event, handler)

    @property
    def local_node_address(self):
        """
        Return the local node's address
        """
        if self.config.node_address is not None:
            return self.config.node_address
        else:
            if self.config.bind_address is not None:
                if self.config.bind_address == '0.0.0.0':
                    default_ip_address = network.default_ip_address()
                    if default_ip_address is None:
                        raise RuntimeError("Unable to determine default IP address")
                    return default_ip_address
                else:
                    return self.config.bind_address
            else:
                return self._udp_listener.local_address

    @property
    def local_node_port(self):
        """
        Return the local node's port
        """
        if self.config.node_port is not None:
            return self.config.node_port
        else:
            if self.config.bind_port is not None:
                return self.config.bind_port
            else:
                return self._udp_listener.local_port

    @property
    def local_node_name(self):
        """
        Return the local node's name
        """
        return self.config.node_name

    async def _do_sync(self):
        """
        Handle the sync_schedule periodic callback
        """

        def _find_nodes(n):
            return n.name != self.local_node_name and n.status != state.NODE_STATUS_DEAD

        sync_node = next(iter(state.select_random_nodes(self.config.sync_nodes, self._nodes, _find_nodes)), None)
        if sync_node is None:
            return

        LOG.debug("Syncing node: %s", sync_node)

        try:
            await self._sync_node(sync_node)
        except Exception:
            LOG.exception("Error running sync")

    async def _do_probe(self):
        """
        Handle the probe_schedule periodic callback
        """
        node = None
        checked = 0

        # only check as many nodes as exist
        while checked < len(self._nodes):

            # handle wrap around
            if self._probe_index >= len(self._nodes):
                self._probe_index = 0
                continue

            node = self._nodes[self._probe_index]

            if node == self._nodes.local_node:
                skip = True  # skip local node
            elif node.status == state.NODE_STATUS_DEAD:
                skip = True  # skip dead nodes
            else:
                skip = False

            self._probe_index += 1

            # keep checking
            if skip:
                checked += 1
                node = None
                continue

            break

        if node is None:
            return

        try:
            # send ping messages
            self._loop.create_task(self._probe_node(node))
        except Exception:
            LOG.exception("Error running probe")

    async def _probe_node(self, target_node: state.Node):
        LOG.debug("Probing node: %s", target_node)

        # get a sequence number for the ping
        next_seq = self._probe_seq.increment()

        # start waiting for probe result
        waiter = self._wait_for_probe(next_seq)

        # send ping message
        ping = messages.PingMessage(next_seq,
                                    target=target_node.name,
                                    sender=self.local_node_name,
                                    sender_addr=messages.InternetAddress(self.local_node_address,
                                                                         self.local_node_port))
        LOG.debug("Sending PING (seq=%d) to %s", ping.seq, target_node.name)
        await self._send_udp_message(target_node.host, target_node.port, ping)

        # wait for probe result or timeout
        result = False
        try:
            result = await waiter
        except asyncio.TimeoutError:
            await self._handle_probe_timeout(target_node)
        else:
            await self._handle_probe_result(target_node, result)

        # if probe was successful no need to send indirect probe
        if result:
            return

        # if probe failed, send indirect probe
        try:
            result = await self._probe_node_indirect(target_node, self.config.probe_indirect_nodes)
        except Exception:
            LOG.exception("Error running indirect probe")

    async def _handle_probe_result(self, node: state.Node, result: bool):
        if result:
            LOG.debug("Probe successful for node: %s result=%s", node.name, result)

            # if node is suspect notify that node is alive
            if node.status == state.NODE_STATUS_SUSPECT:
                LOG.warn("Suspect node is alive: %s", node.name)

                await self._nodes.on_node_alive(node.name, node.incarnation, node.host, node.port)

        else:
            # TODO: handle NACK
            LOG.debug("Probe failed for node: %s result=%s", node.name, result)
            raise NotImplementedError()

    async def _handle_probe_timeout(self, node: state.Node):
        LOG.debug("Probe failed for node: %s", node.name)

        # notify node is suspect
        await self._nodes.on_node_suspect(node.name, node.incarnation)

    async def _wait_for_probe(self, seq: int) -> bool:

        # start a timer
        start_time = time.time()

        # create a Future
        future = self._loop.create_future()

        # save future to be resolved when an ack is received
        self._probe_status[seq] = future

        try:
            # wait for timeout
            LOG.trace("Waiting for probe (seq=%d)", seq)
            result = await asyncio.wait_for(future, timeout=self.config.probe_timeout)
        except asyncio.TimeoutError:
            end_time = time.time()
            LOG.trace("Timeout waiting for probe (seq=%d) elapsed time: %0.2f", seq, end_time - start_time)
            raise  # re-raise TimeoutError
        finally:
            # remove pending probe status
            del self._probe_status[seq]

        end_time = time.time()
        LOG.trace("Successful probe (seq=%d) elapsed time: %0.2fs", seq, end_time - start_time)

        return result

    async def _probe_node_indirect(self, target_node: state.Node, k: int):

        def _find_nodes(n):
            return n.name != target_node.name and n.name != self.local_node_name and n.status == state.NODE_STATUS_ALIVE

        # send indirect ping to k nodes
        probes = []
        for indirect_node in state.select_random_nodes(k, self._nodes, _find_nodes):
            probes.append(self._probe_node_indirect_via(target_node, indirect_node))

        try:
            # TODO: check results
            await asyncio.gather(*probes, loop=self._loop)
        except:
            LOG.exception("Error probing nodes")

    async def _probe_node_indirect_via(self, target_node: state.Node, indirect_node: state.Node):
        LOG.debug("Probing node: %s indirectly via %s", target_node.name, indirect_node.name)

        # get a sequence number for the ping
        next_seq = self._probe_seq.increment()

        # start waiting for probe result
        waiter = self._wait_for_probe(next_seq)

        # send ping request message
        ping_req = messages.PingRequestMessage(next_seq,
                                               target=target_node.name,
                                               target_addr=messages.InternetAddress(target_node.host,
                                                                                    target_node.port),
                                               sender=self.local_node_name,
                                               sender_addr=messages.InternetAddress(self.local_node_address,
                                                                                    self.local_node_port))

        LOG.debug("Sending PING-REQ (seq=%d) to %s", ping_req.seq, indirect_node.name)
        await self._send_udp_message(indirect_node.host, indirect_node.port, ping_req)

        # wait for probe result or timeout
        try:
            result = await waiter
        except asyncio.TimeoutError:
            LOG.debug("Timeout waiting for indirect ACK %d", next_seq)
            await self._handle_probe_timeout(target_node)
        else:
            await self._handle_probe_result(target_node, result)

    async def _merge_remote_state(self, remote_state):
        LOG.trace("Merging remote state: %s", remote_state)

        # merge Node into state

        if remote_state.status == state.NODE_STATUS_ALIVE:
            await self._nodes.on_node_alive(remote_state.node,
                                            remote_state.incarnation,
                                            remote_state.addr.address,
                                            remote_state.addr.port,
                                            remote_state.metadata)

        elif remote_state.status == state.NODE_STATUS_SUSPECT:
            await self._nodes.on_node_suspect(remote_state.node,
                                              remote_state.incarnation,
                                              remote_state.metadata)

        elif remote_state.status == state.NODE_STATUS_DEAD:
            # rather then declaring a node a dead immediately, mark it as suspect
            await self._nodes.on_node_suspect(remote_state.node,
                                              remote_state.incarnation,
                                              remote_state.metadata)

        else:
            LOG.warn("Unknown node status: %s", remote_state.status)
            return

    async def _send_local_state(self, stream_writer):

        # get local state
        local_state = []
        for node in self._nodes:
            local_state.append(messages.RemoteNodeState(node=node.name,
                                                        addr=messages.InternetAddress(node.host, node.port),
                                                        version=node.version,
                                                        incarnation=node.incarnation,
                                                        status=node.status,
                                                        metadata=node.metadata))

        LOG.trace("Sending local state %s", local_state)

        # send message
        await self._send_tcp_message(messages.SyncMessage(remote_state=local_state), stream_writer)

    async def _after_connect_loop(self, node):
        try:
            # read until closed
            while node.connected:
                try:
                    raw = await self._read_tcp_message(node.read_stream)
                except IOError:
                    LOG.exception("Error reading stream")
                    break

                if raw is None:
                    break

                # decode the message
                message = self._decode_message(raw)
                if message is None:
                    continue

                # dispatch the message
                await self._handle_tcp_client_message(message, node.read_stream, node.write_stream,
                                                      (node.host, node.port))

        except Exception:
            LOG.exception("Error handling TCP stream")
            return

    async def _ensure_connected(self, node):
        if not node.connected:
            await node.connect()
            node._loop = asyncio.ensure_future(self._after_connect_loop(node))

    async def _sync_node(self, node):
        await self._ensure_connected(node)

        # send local state
        try:
            await self._send_local_state(node.write_stream)
        except IOError:
            LOG.exception("Error sending remote state")
            return

    async def _sync_host(self, node_host, node_port):
        """
        Sync with remote node
        """
        connection = None
        try:

            # connect to node
            LOG.debug("Connecting to node %s:%d", node_host, node_port)
            try:
                stream_reader, stream_writer = await asyncio.open_connection(node_host, node_port)
            except Exception:
                LOG.exception("Error connecting to node %s:%d", node_host, node_port)
                return

            # send local state
            try:
                await self._send_local_state(stream_writer)
            except IOError:
                LOG.exception("Error sending remote state")
                return

            # read remote state
            try:
                remote_sync_message = self._decode_message((await self._read_tcp_message(stream_reader)))
            except IOError:
                LOG.exception("Error receiving remote state")
                return

            # merge remote state
            for remote_state in remote_sync_message.nodes:
                await self._merge_remote_state(remote_state)

        except:
            LOG.exception("Error syncing node")
            return

        finally:
            if connection is not None:
                connection.close()

        return True

    def _decode_message_header(self, raw):
        return struct.unpack(messages.MESSAGE_HEADER_FORMAT, raw)

    def _decode_message(self, raw):
        try:
            msg = messages.MessageSerializer.decode(raw, encryption=[self.config.encryption_key])
            return msg
        except messages.MessageDecodeError as e:
            LOG.error("Error decoding message: %s", e)
            return

    def _encode_message(self, msg):
        data = messages.MessageSerializer.encode(msg, encryption=self.config.encryption_key)
        LOG.trace("Encoded message: %s (%d bytes)", msg, len(data))
        return data

    # ------------------- TCP Message Handling -------------------

    async def _handle_tcp_connection(self,
                                     stream_reader: asyncio.streams.StreamReader,
                                     stream_writer: asyncio.streams.StreamWriter,
                                     client_addr):

        try:
            # read until closed
            while True:

                try:
                    raw = await self._read_tcp_message(stream_reader)
                except IOError:
                    LOG.exception("Error reading stream")
                    break

                if raw is None:
                    break

                # decode the message
                message = self._decode_message(raw)
                if message is None:
                    continue

                # dispatch the message
                await self._handle_tcp_message(message, stream_reader, stream_writer, client_addr)

        except Exception:
            LOG.exception("Error handling TCP stream")
            return

    async def _read_tcp_message(self, stream_reader: asyncio.streams.StreamReader):
        """
        Read a message from a stream asynchronously
        """
        buf = bytes()
        data = await stream_reader.read(messages.MESSAGE_HEADER_LENGTH)
        if not data:
            return
        buf += data
        length, _, _, = self._decode_message_header(buf)
        buf += await stream_reader.read(length - messages.MESSAGE_HEADER_LENGTH)
        return buf

    async def _send_tcp_message(self, message, stream_writer: asyncio.streams.StreamWriter):
        """
        Write a message to a stream asynchronously
        """
        LOG.trace("Sending %s", message)

        # encode message
        buf = bytes()
        buf += self._encode_message(message)

        # send message
        stream_writer.write(buf)

    async def _handle_tcp_message(self, message, stream_reader, stream_writer, client_addr):
        LOG.trace("Handling TCP message from %s", client_addr)
        try:
            if isinstance(message, messages.SyncMessage):
                # noinspection PyTypeChecker
                await self._handle_sync_message(message, stream_reader, stream_writer, client_addr)
            elif isinstance(message, messages.UserMessage):
                await self._handle_user_message(message, client_addr)
            else:
                LOG.warn("Unknown message type: %r", message.__class__)
                return
        except Exception:
            LOG.exception("Error dispatching TCP message")
            return

    async def _handle_tcp_client_message(self, message, stream_reader, _, client_addr):
        LOG.trace("Handling TCP message from %s", client_addr)
        try:
            if isinstance(message, messages.SyncMessage):
                # noinspection PyTypeChecker

                # merge remote state
                for remote_state in message.nodes:
                    await self._merge_remote_state(remote_state)

            elif isinstance(message, messages.UserMessage):
                await self._handle_user_message(message, client_addr)
            else:
                LOG.warn("Unknown message type: %r", message.__class__)
                return
        except Exception:
            LOG.exception("Error dispatching TCP message")
            return

    async def _handle_sync_message(self, message, stream_reader, stream_writer, client_addr):
        LOG.trace("Handling SYNC message: nodes=%s", message.nodes)

        # merge remote state
        for remote_state in message.nodes:
            await self._merge_remote_state(remote_state)

        # reply with our state
        try:
            await self._send_local_state(stream_writer)
        except IOError:
            LOG.exception("Error sending remote state")
            return

    # ------------------- UDP Message Handling -------------------

    def _read_udp_message(self, reader):
        """
        Read a message from a UDP stream synchronously
        """
        buf = bytes()
        buf += reader.read(messages.MESSAGE_HEADER_LENGTH)
        if not buf:
            return None
        length, _, _, = self._decode_message_header(buf)
        buf += reader.read(length - messages.MESSAGE_HEADER_LENGTH)
        return buf

    async def _handle_udp_data(self, data, client_addr):
        try:

            # create a buffered reader
            reader = io.BufferedReader(io.BytesIO(data))

            # read until closed
            while True:

                # read a message
                try:
                    raw = self._read_udp_message(reader)
                    if raw is None:
                        break
                except IOError:
                    LOG.exception("Error reading stream")
                    break

                # decode the message
                msg = self._decode_message(raw)
                if msg is None:
                    continue

                # dispatch the message
                await self._handle_udp_message(msg, client_addr)

        except Exception:
            LOG.exception("Error handling UDP data")
            return

    # noinspection PyTypeChecker
    async def _handle_udp_message(self, msg, client_addr):
        LOG.trace("Handling UDP message from %s:%d", *client_addr)
        try:
            if isinstance(msg, messages.AliveMessage):
                await self._handle_alive_message(msg, client_addr)
            elif isinstance(msg, messages.SuspectMessage):
                await self._handle_suspect_message(msg, client_addr)
            elif isinstance(msg, messages.DeadMessage):
                await self._handle_dead_message(msg, client_addr)
            elif isinstance(msg, messages.PingMessage):
                await self._handle_ping_message(msg, client_addr)
            elif isinstance(msg, messages.PingRequestMessage):
                await self._handle_ping_request_message(msg, client_addr)
            elif isinstance(msg, messages.AckMessage):
                await self._handle_ack_message(msg, client_addr)
            elif isinstance(msg, messages.NackMessage):
                await self._handle_nack_message(msg, client_addr)
            elif isinstance(msg, messages.UserMessage):
                await self._handle_user_message(msg, client_addr)
            else:
                LOG.warn("Unknown message type: %r", msg.__class__)
                return
        except:
            LOG.exception("Error dispatching UDP message")
            return

    # noinspection PyUnusedLocal
    async def _handle_alive_message(self, msg, client_addr):
        LOG.trace("Handling ALIVE message: node=%s", msg.node)

        await self._nodes.on_node_alive(msg.node, msg.incarnation, msg.addr.address, msg.addr.port, msg.metadata)

    # noinspection PyUnusedLocal
    async def _handle_suspect_message(self, msg, client_addr):
        LOG.trace("Handling SUSPECT message: node=%s", msg.node)

        await self._nodes.on_node_suspect(msg.node, msg.incarnation, {})

    # noinspection PyUnusedLocal
    async def _handle_dead_message(self, msg, client_addr):
        LOG.trace("Handling DEAD message: node=%s", msg.node)

        await self._nodes.on_node_dead(msg.node, msg.incarnation)

    # noinspection PyUnusedLocal
    async def _handle_ping_message(self, msg, client_addr):
        LOG.trace("Handling PING message: target=%s", msg.node)

        # ensure target node is local node
        if msg.node != self.local_node_name:
            LOG.warn("Received ping message %d from %s for non-local node.", msg.seq, msg.sender_addr)
            return

        # send ack message
        ack = messages.AckMessage(msg.seq, sender=self.local_node_name)
        LOG.debug("Sending ACK (%d) to %s", msg.seq, msg.sender_addr)
        await self._send_udp_message(msg.sender_addr.address, msg.sender_addr.port, ack)

    # noinspection PyUnusedLocal
    async def _handle_ping_request_message(self, msg, client_addr):
        LOG.trace("Handling PING-REQ (%d): target=%s", msg.seq, msg.node)

        # get a sequence number for the ping
        next_seq = self._probe_seq.increment()

        # start waiting for probe result
        waiter = self._wait_for_probe(next_seq)

        # create PingMessage
        ping = messages.PingMessage(next_seq,
                                    target=msg.node,
                                    sender=self.local_node_name,
                                    sender_addr=messages.InternetAddress(self.local_node_address,
                                                                         self.local_node_port))

        LOG.debug("Sending PING (%d) to %s in response to PING-REQ (%d)", next_seq, msg.node_addr, msg.seq)
        await self._send_udp_message(msg.node_addr.address, msg.node_addr.port, ping)

        # wait for probe result or timeout
        try:
            result = await waiter
        except asyncio.TimeoutError:
            LOG.debug("Timeout waiting for ACK %d", next_seq)
            # TODO: send nack
            # await self._forward_indirect_probe_timeout(msg)
        else:
            await self._forward_indirect_probe_result(msg)

    # noinspection PyUnusedLocal
    async def _handle_ack_message(self, msg, client_addr):
        LOG.trace("Handling ACK message (%d): sender=%s", msg.seq, msg.sender)

        # resolve pending probe
        ack_seq = msg.seq
        if ack_seq in self._probe_status:
            LOG.trace("Resolving probe (seq=%d) result=%s", msg.seq, True)
            self._probe_status[ack_seq].set_result(True)
        else:
            LOG.warn("Received ACK for unknown probe: %d from %s", msg.seq, msg.sender)

    # noinspection PyUnusedLocal
    async def _handle_nack_message(self, msg, client):
        LOG.trace("Handling NACK message (%d): sender=%s", msg.seq, msg.sender)

        # resolve pending probe
        ack_seq = msg.seq
        if ack_seq in self._probe_status:
            LOG.trace("Resolving probe (seq=%d) result=%s", msg.seq, False)
            self._probe_status[ack_seq].set_result(False)
        else:
            LOG.warn("Received NACK for unknown probe: %d from %s", msg.seq, msg.sender)

    async def _forward_indirect_probe_result(self, msg):
        # create AckMessage
        ack = messages.AckMessage(msg.seq, sender=self.local_node_name)
        LOG.debug("Forwarding ACK (%d) to %s", msg.seq, msg.node)
        await self._send_udp_message(msg.sender_addr.address, msg.sender_addr.port, ack)

    async def _forward_indirect_probe_timeout(self, msg):
        # create NackMessage
        ack = messages.NackMessage(msg.seq, sender=self.local_node_name)
        LOG.debug("Forwarding NACK (%d) to %s", msg.seq, msg.node)
        await self._send_udp_message(msg.sender_addr.address, msg.sender_addr.port, ack)

    # noinspection PyUnusedLocal
    async def _handle_user_message(self, msg, client):
        LOG.trace("Handling USER message (%d bytes) sender=%s", len(msg.data), msg.sender)
        if self._user_message_callback is not None:
            try:
                await self._user_message_callback(msg, client)
            except Exception as e:
                LOG.error("Error handling user message", exc_info=e)

    async def _send_udp_message(self, host, port, msg):
        LOG.trace("Sending %s to %s:%d", msg, host, port)

        # encode message
        buf = bytes()
        buf += self._encode_message(msg)

        # max_messages = len(self._nodes)
        max_transmits = _calculate_transmit_limit(len(self._nodes), self.config.retransmit_multi)
        max_bytes = 512 - len(buf)

        # gather gossip messages (already encoded)
        gossip = self._queue.fetch(max_transmits, max_bytes)
        if gossip:
            LOG.trace("Gossip message max-transmits: %d", max_transmits)

        for g in gossip:
            LOG.trace("Piggy-backing message %s to %s:%d", self._decode_message(g), host, port)
            buf += g

        # send message
        self._udp_listener.sendto(buf, host, port)
