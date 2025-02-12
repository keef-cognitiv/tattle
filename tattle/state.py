import asyncio
import collections
import math
import random
import time

from . import logging
from . import messages
from . import timer
from . import sequence

__all__ = [
    'NodeManager',
    'NODE_STATUS_ALIVE',
    'NODE_STATUS_DEAD',
    'NODE_STATUS_SUSPECT',
    'select_random_nodes'
]

NODE_STATUS_ALIVE = 'ALIVE'
NODE_STATUS_SUSPECT = 'SUSPECT'
NODE_STATUS_DEAD = 'DEAD'

LOG = logging.get_logger(__name__)


def _calculate_suspicion_timeout(n, interval):
    """
    Calculate initial suspicion timeout

    :param n: number of nodes
    :param interval: probe interval in seconds
    :return: timeout in seconds
    """
    scale = max(1, math.log10(max(1, n)))
    return scale * interval


def _update_suspicion_timeout(n, k, elapsed, max_timeout, min_timeout):
    """
    Calculate a new suspicion timeout after n confirmations

    :param n: number of confirmations received
    :param k: number of confirmation expected
    :param max_timeout: max timeout in seconds
    :param min_timeout: min timeout in seconds
    :return:
    """
    ratio = math.log10(n + 1) / math.log10(k + 1)
    timeout = max_timeout - ratio * (max_timeout - min_timeout)
    timeout = max(min_timeout, timeout)
    return timeout - elapsed


def _calculate_expected_confirmations(n, multi):
    """
    Calculate number of expected confirmations

    :param n: number of nodes
    :return:
    """
    k = multi - 2
    if n - 2 < k:
        k = 0
    return k


def select_random_nodes(k, nodes, predicate=None):
    selected = []

    k = min(k, len(nodes))
    c = 0

    while len(selected) < k and c <= (3 * len(nodes)):
        c += 1
        node = random.choice(nodes)
        if node in selected:
            continue

        if predicate is not None:
            if not predicate(node):
                continue

        selected.append(node)

    return selected


class Node(object):
    def __init__(self, name, host, port, incarnation=0, status=NODE_STATUS_DEAD):
        self.name = name
        self.host = host
        self.port = port
        self.incarnation = incarnation
        self.version = 1
        self.metadata = dict()
        self._status = status
        self._status_change_timestamp = None
        self.read_stream = None
        self.write_stream = None
        self._loop = None

    def _get_status(self):
        return self._status

    def _set_status(self, value):
        if value != self._status:
            self._status = value
            self._status_change_timestamp = time.time()

    status = property(_get_status, _set_status)

    def __repr__(self):
        return "<Node %s status:%s>" % (self.name, self.status)

    async def connect(self):
        if (self.read_stream is not None and self.read_stream.exception() is not None) or \
                (self.write_stream is not None and self.write_stream.exception() is not None):
            await self.close()

        if self.read_stream is None or self.write_stream is None:
            self.read_stream, self.write_stream = await asyncio.open_connection(self.host, self.port)

    @property
    def connected(self):
        return self.read_stream is not None and self.read_stream.exception() is None and \
            self.write_stream is not None and not self.write_stream.is_closing()

    async def close(self):
        if self._loop:
            self._loop.cancel()

        if self.read_stream is not None:
            self.read_stream = None

        if self.write_stream is not None:
            self.write_stream.close()
            self.write_stream = None


SuspectNode = collections.namedtuple('SuspectNode', ['timer', 'k', 'min_timeout', 'max_timeout', 'confirmations'])


class NodeManager(collections.abc.Sequence, collections.abc.Mapping):
    """
    The NodeManager manages the membership state of the Cluster.
    """

    def __init__(self, config, queue, events, loop=None):
        """
        Initialize instance of the NodeManager class

        :param config: config object
        :param queue: broadcast queue
        :type config: tattle.config.Configuration
        :type events: tattle.event.EventManager
        :type queue: tattle.queue.BroadcastQueue
        """
        self.config = config
        self._queue = queue
        self._events = events
        self._loop = loop or asyncio.get_event_loop()
        self._leaving = False
        self._nodes = list()
        self._nodes_map = dict()
        self._nodes_lock = asyncio.Lock()
        self._suspect_nodes = dict()
        self._local_node_name = None
        self._local_node_seq = sequence.Sequence()

    def __getitem__(self, index):
        if isinstance(index, str):
            return self._nodes_map[index]
        else:
            return self._nodes[index]

    def __iter__(self):
        return self._nodes.__iter__()

    def __len__(self):
        return len(self._nodes)

    def _swap_random_nodes(self):
        random_index = random.randint(0, len(self._nodes) - 1)
        random_node = self._nodes[random_index]
        last_node = self._nodes[len(self._nodes) - 1]
        self._nodes[random_index] = last_node
        self._nodes[len(self._nodes) - 1] = random_node

    @property
    def local_node(self):
        return self._nodes_map.get(self._local_node_name)

    async def set_local_node(self, local_node_name, local_node_host, local_node_port, metadata):
        # assert self._local_node_name is None
        self._local_node_name = local_node_name

        # generate incarnation for the node
        incarnation = self._local_node_seq.increment()

        # signal node is alive
        await self.on_node_alive(local_node_name, incarnation, local_node_host, local_node_port, metadata, bootstrap=True)

    async def leave_local_node(self):
        assert self._local_node_name is not None
        self._leaving = True

        # signal node is dead
        await self.on_node_dead(self.local_node.name, self.local_node.incarnation)

    async def on_node_alive(self, name, incarnation, host, port, metadata, bootstrap=False):
        """
        Handle a Node alive notification
        """

        # acquire node lock
        async with self._nodes_lock:

            # # It is possible that during a leave, there is already an alive message in in the queue.
            # # If that happens ignore it so we don't rejoin the cluster.
            # if name == self.local_node.name and self._leaving:
            #     return

            # check if this is a new node
            current_node = self._nodes_map.get(name)
            if current_node is None:
                LOG.debug("Node discovered: %s", name)

                # create new Node
                current_node = Node(name, host, port)

                # save current state
                self._nodes_map[name] = current_node
                self._nodes.append(current_node)

                # swap new node with a random node to ensure detection of failed node is bounded
                self._swap_random_nodes()

            LOG.debug("Node alive %s (current incarnation: %d, new incarnation: %d)",
                      current_node.name,
                      current_node.incarnation,
                      incarnation)

            # return if conflicting node address
            if current_node.host != host or current_node.port != port:
                LOG.error("Conflicting node address for %s (current=%s:%d new=%s:%d)",
                          name, current_node.host, current_node.port, host, port)
                return

            is_local_node = current_node is self.local_node

            # if this is not about us, return if the incarnation number is older or the same at the current state
            if not is_local_node and incarnation <= current_node.incarnation:
                LOG.trace("%s is older then current state: %d <= %d", name,
                          incarnation, current_node.incarnation)
                return

            # if this is about us, return if the incarnation number is older then the current state
            if is_local_node and incarnation < current_node.incarnation:
                LOG.trace("%s is older then current state: %d < %d", name,
                          incarnation, current_node.incarnation)
                return

            # if the node is suspect, alive message cancels the suspicion
            if current_node.status == NODE_STATUS_SUSPECT:
                await self._cancel_suspect_node(current_node)

            old_status = current_node.status

            # update the current node status and incarnation number
            current_node.incarnation = incarnation
            current_node.status = NODE_STATUS_ALIVE

            current_node.metadata = current_node.metadata | metadata

            # broadcast alive message
            self._broadcast_alive(current_node)

            # emit 'node.alive' if node status changed
            if old_status != NODE_STATUS_ALIVE:
                self._events.emit('node.alive', current_node)

                LOG.info("Node alive: %s (incarnation %d)", name, incarnation)

    async def on_node_dead(self, name, incarnation):
        """
        Handle a Node dead notification
        """

        # acquire node lock
        async with self._nodes_lock:

            # bail if this is a new node
            current_node = self._nodes_map.get(name)
            if current_node is None:
                LOG.warn("Ignoring unknown node: %s", name)
                return

            # return if node is dead
            if current_node.status == NODE_STATUS_DEAD:
                LOG.trace("Ignoring DEAD node: %s", name)
                return

            # return if the incarnation number is older then the current state
            if incarnation < current_node.incarnation:
                LOG.trace("%s is older then current state: %d < %d", current_node.name, incarnation,
                          current_node.incarnation)
                return

            # if this is about the local node, we need to refute. otherwise broadcast it
            if current_node is self.local_node and not self._leaving:
                LOG.warn("Refuting DEAD message (incarnation=%d)", incarnation)
                self._refute()
                return

            # if the node is suspect, alive message cancels the suspicion
            if current_node.status == NODE_STATUS_SUSPECT:
                await self._cancel_suspect_node(current_node)

            old_status = current_node.status

            # update the current node status and incarnation number
            current_node.incarnation = incarnation
            current_node.status = NODE_STATUS_DEAD

            # broadcast dead message
            self._broadcast_dead(current_node)

            # emit 'node.dead' if node status changed
            if old_status != NODE_STATUS_DEAD:
                self._events.emit('node.dead', current_node)

                LOG.error("Node dead: %s (incarnation %d)", name, incarnation)

    async def on_node_suspect(self, name, incarnation, metadata):
        """
        Handle a Node suspect notification
        """

        # acquire node lock
        async with self._nodes_lock:

            # bail if this is a new node
            current_node = self._nodes_map.get(name)
            if current_node is None:
                LOG.warn("Ignoring unknown node: %s", name)
                return

            # return if node is dead
            if current_node.status == NODE_STATUS_DEAD:
                LOG.trace("Ignoring DEAD node: %s", name)
                return

            # return if the incarnation number is older then the current state
            if incarnation < current_node.incarnation:
                LOG.trace("%s is older then current state: %d < %d", current_node.name, incarnation,
                          current_node.incarnation)
                return

            # if this is about the local node, we need to refute. otherwise broadcast it
            if current_node is self.local_node:
                LOG.warn("Refuting SUSPECT message (incarnation=%d)", incarnation)
                self._refute()  # don't mark ourselves suspect
                return

            # check if node is currently under suspicion
            if current_node.status == NODE_STATUS_SUSPECT:
                # TODO: confirm suspect node
                pass

            old_status = current_node.status

            # update the current node status and incarnation number
            current_node.incarnation = incarnation
            current_node.status = NODE_STATUS_SUSPECT

            # create suspect node
            await self._create_suspect_node(current_node)

            current_node.metadata = current_node.metadata | metadata

            # broadcast suspect message
            self._broadcast_suspect(current_node)

            # emit 'node.suspect' if node status changed
            if old_status != NODE_STATUS_SUSPECT:
                self._events.emit('node.suspect', current_node)

            if old_status != NODE_STATUS_SUSPECT:
                LOG.warn("Node suspect: %s (incarnation %d)", name, incarnation)


    async def _confirm_suspect_node(self, node):
        pass

    async def _create_suspect_node(self, node):

        async def _handle_suspect_timer():
            LOG.debug("Suspect timer expired for %s", node.name)
            await self.on_node_dead(node.name, node.incarnation)

        # ignore if pending timer exists
        if node.name in self._suspect_nodes:
            return

        n = len(self._nodes)
        k = _calculate_expected_confirmations(n, self.config.suspicion_min_timeout_multi)
        interval = self.config.probe_interval
        min_timeout = self.config.suspicion_min_timeout_multi * _calculate_suspicion_timeout(n, interval)
        max_timeout = self.config.suspicion_max_timeout_multi * min_timeout

        if k < 1:
            timeout = max_timeout
        else:
            timeout = min_timeout

        # create a Timer
        timer_ = timer.Timer(_handle_suspect_timer, timeout, self._loop)

        LOG.debug("Starting suspicion timer: timeout=%0.4f k=%d min=%0.4f max=%0.4f",
                  timeout,
                  k,
                  min_timeout,
                  max_timeout)

        # add node to suspects
        self._suspect_nodes[node.name] = SuspectNode(timer_, k, min_timeout, max_timeout, dict())
        timer_.start()

        LOG.debug("Created suspect node: %s", node.name)

    async def _cancel_suspect_node(self, node):
        # stop suspect Timer
        suspect = self._suspect_nodes[node.name]
        suspect.timer.stop()

        # remove node from suspects
        del self._suspect_nodes[node.name]

        LOG.debug("Canceled suspect node: %s", node.name)

    def _send_broadcast(self, node, msg):
        self._queue.push(node.name, messages.MessageSerializer.encode(msg, encryption=self.config.encryption_key))
        LOG.trace("Queued message: %s", msg)

    def _broadcast_alive(self, node):
        LOG.debug("Broadcasting ALIVE message for node: %s (incarnation=%d)", node.name, node.incarnation)
        alive = messages.AliveMessage(node=node.name,
                                      addr=messages.InternetAddress(node.host, node.port),
                                      incarnation=node.incarnation,
                                      metadata=node.metadata)
        self._send_broadcast(node, alive)

    def _broadcast_suspect(self, node):
        LOG.debug("Broadcasting SUSPECT message for node: %s (incarnation=%d)", node.name, node.incarnation)
        suspect = messages.SuspectMessage(node=node.name, incarnation=node.incarnation, sender=self.local_node.name)
        self._send_broadcast(node, suspect)

    def _broadcast_dead(self, node):
        LOG.debug("Broadcasting DEAD message for node: %s (incarnation=%d)", node.name, node.incarnation)
        dead = messages.DeadMessage(node=node.name, incarnation=node.incarnation, sender=self.local_node.name)
        self._send_broadcast(node, dead)

    def _refute(self):

        # increment local node incarnation
        self.local_node.incarnation = self._local_node_seq.increment()
        LOG.debug("Refuting message (new incarnation %d)", self.local_node.incarnation)

        # broadcast alive message
        self._broadcast_alive(self.local_node)
