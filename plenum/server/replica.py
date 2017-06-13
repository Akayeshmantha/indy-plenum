import time
from binascii import hexlify
from collections import deque, OrderedDict
from enum import IntEnum
from enum import unique
from operator import itemgetter
from typing import Dict, List, Union
from typing import Optional, Any
from typing import Set
from typing import Tuple
from hashlib import sha256

from orderedset import OrderedSet
from sortedcontainers import SortedDict
from sortedcontainers import SortedList

import plenum.server.node
from plenum.common.config_util import getConfig
from plenum.common.exceptions import SuspiciousNode, \
    InvalidClientMessageException, UnknownIdentifier
from plenum.common.signing import serialize
from plenum.common.types import PrePrepare, \
    Prepare, Commit, Ordered, ThreePhaseMsg, ThreePhaseKey, ThreePCState, \
    CheckpointState, Checkpoint, Reject, f, InstanceChange
from plenum.common.request import ReqDigest, Request, ReqKey
from plenum.common.message_processor import MessageProcessor
from plenum.common.util import updateNamedTuple, compare_3PC_keys, max_3PC_key
from stp_core.common.log import getlogger
from plenum.server.has_action_queue import HasActionQueue
from plenum.server.models import Commits, Prepares
from plenum.server.router import Router
from plenum.server.suspicion_codes import Suspicions

logger = getlogger()

LOG_TAGS = {
    'PREPREPARE': {"tags": ["node-preprepare"]},
    'PREPARE': {"tags": ["node-prepare"]},
    'COMMIT': {"tags": ["node-commit"]},
    'ORDERED': {"tags": ["node-ordered"]}
}


@unique
class TPCStat(IntEnum):  # TPC => Three-Phase Commit
    ReqDigestRcvd = 0
    PrePrepareSent = 1
    PrePrepareRcvd = 2
    PrepareRcvd = 3
    PrepareSent = 4
    CommitRcvd = 5
    CommitSent = 6
    OrderSent = 7


class Stats:
    def __init__(self, keys):
        sort = sorted([k.value for k in keys])
        self.stats = OrderedDict((s, 0) for s in sort)

    def inc(self, key):
        """
        Increment the stat specified by key.
        """
        self.stats[key] += 1

    def get(self, key):
        return self.stats[key]

    def __repr__(self):
        return OrderedDict((TPCStat(k).name, v)
                           for k, v in self.stats.items())


class Replica(HasActionQueue, MessageProcessor):
    def __init__(self, node: 'plenum.server.node.Node', instId: int,
                 isMaster: bool = False):
        """
        Create a new replica.

        :param node: Node on which this replica is located
        :param instId: the id of the protocol instance the replica belongs to
        :param isMaster: is this a replica of the master protocol instance
        """
        HasActionQueue.__init__(self)
        self.stats = Stats(TPCStat)

        self.config = getConfig()

        routerArgs = [(ReqKey, self.readyFor3PC)]

        for r in [PrePrepare, Prepare, Commit]:
            routerArgs.append((r, self.processThreePhaseMsg))

        routerArgs.append((Checkpoint, self.processCheckpoint))
        routerArgs.append((ThreePCState, self.process3PhaseState))

        self.inBoxRouter = Router(*routerArgs)

        self.threePhaseRouter = Router(
                (PrePrepare, self.processPrePrepare),
                (Prepare, self.processPrepare),
                (Commit, self.processCommit)
        )

        self.node = node
        self.instId = instId

        self.name = self.generateName(node.name, self.instId)

        self.outBox = deque()
        """
        This queue is used by the replica to send messages to its node. Replica
        puts messages that are consumed by its node
        """

        self.inBox = deque()
        """
        This queue is used by the replica to receive messages from its node.
        Node puts messages that are consumed by the replica
        """

        self.inBoxStash = deque()
        """
        If messages need to go back on the queue, they go here temporarily and
        are put back on the queue on a state change
        """

        self.isMaster = isMaster

        # Indicates name of the primary replica of this protocol instance.
        # None in case the replica does not know who the primary of the
        # instance is
        self._primaryName = None    # type: Optional[str]

        # TODO: Rename since it will contain all messages till primary is
        # selected, primary selection is only done once pool ledger is
        # caught up
        # Requests waiting to be processed once the replica is able to decide
        # whether it is primary or not
        self.postElectionMsgs = deque()

        # PRE-PREPAREs that are waiting to be processed but do not have the
        # corresponding request finalised. Happens when replica has not been
        # forwarded the request by the node but is getting 3 phase messages.
        # The value is a list since a malicious entry might send PRE-PREPARE
        # with a different digest and since we dont have the request finalised
        # yet, we store all PRE-PPREPAREs
        self.prePreparesPendingFinReqs = []   # type: List[Tuple[PrePrepare, str, Set[Tuple[str, int]]]]

        # PrePrepares waiting for previous PrePrepares, key being tuple of view
        # number and pre-prepare sequence numbers and value being tuple of
        # PrePrepare and sender
        # TODO: Since pp_seq_no will start from 1 in each view, the comparator
        # of SortedDict needs to change
        self.prePreparesPendingPrevPP = SortedDict(lambda k: (k[0], k[1]))

        # PREPAREs that are stored by non primary replica for which it has not
        #  got any PRE-PREPARE. Dictionary that stores a tuple of view no and
        #  prepare sequence number as key and a deque of PREPAREs as value.
        # This deque is attempted to be flushed on receiving every
        # PRE-PREPARE request.
        self.preparesWaitingForPrePrepare = {}
        # type: Dict[Tuple[int, int], deque]

        # COMMITs that are stored for which there are no PRE-PREPARE or PREPARE
        # received
        self.commitsWaitingForPrepare = {}
        # type: Dict[Tuple[int, int], deque]

        # Dictionary of sent PRE-PREPARE that are stored by primary replica
        # which it has broadcasted to all other non primary replicas
        # Key of dictionary is a 2 element tuple with elements viewNo,
        # pre-prepare seqNo and value is the received PRE-PREPARE
        self.sentPrePrepares = SortedDict(lambda k: (k[0], k[1]))
        # type: Dict[Tuple[int, int], PrePrepare]

        # Dictionary of received PRE-PREPAREs. Key of dictionary is a 2
        # element tuple with elements viewNo, pre-prepare seqNo and value
        # is the received PRE-PREPARE
        self.prePrepares = SortedDict(lambda k: (k[0], k[1]))
        # type: Dict[Tuple[int, int], PrePrepare]

        # Dictionary of received Prepare requests. Key of dictionary is a 2
        # element tuple with elements viewNo, seqNo and value is a 2 element
        # tuple containing request digest and set of sender node names(sender
        # replica names in case of multiple protocol instances)
        # (viewNo, seqNo) -> ((identifier, reqId), {senders})
        self.prepares = Prepares()
        # type: Dict[Tuple[int, int], Tuple[Tuple[str, int], Set[str]]]

        self.commits = Commits()
        # type: Dict[Tuple[int, int], Tuple[Tuple[str, int], Set[str]]]

        # Set of tuples to keep track of ordered requests. Each tuple is
        # (viewNo, ppSeqNo).
        self.ordered = OrderedSet()        # type: OrderedSet[Tuple[int, int]]

        # Dictionary to keep track of the which replica was primary during each
        # view. Key is the view no and value is the name of the primary
        # replica during that view
        self.primaryNames = OrderedDict()  # type: OrderedDict[int, str]

        # Holds tuple of view no and prepare seq no of 3-phase messages it
        # received while it was not participating
        self.stashingWhileCatchingUp = set()       # type: Set[Tuple]

        # Commits which are not being ordered since commits with lower
        # sequence numbers have not been ordered yet. Key is the
        # viewNo and value a map of pre-prepare sequence number to commit
        self.stashed_out_of_order_commits = {}  # type: Dict[int,Dict[int,Commit]]

        self.checkpoints = SortedDict(lambda k: k[1])

        self.stashedRecvdCheckpoints = {}   # type: Dict[Tuple,
        # Dict[str, Checkpoint]]

        self.stashingWhileOutsideWaterMarks = deque()

        # Low water mark
        self._h = 0              # type: int
        # Set high water mark (`H`) too
        self.h = 0   # type: int

        self._lastPrePrepareSeqNo = self.h  # type: int

        # Queues used in PRE-PREPARE for each ledger,
        self.requestQueues = {}  # type: Dict[int, deque]
        for ledger_id in self.ledger_ids:
            # Using ordered set since after ordering each PRE-PREPARE,
            # the request key is removed, so fast lookup and removal of
            # request key is needed. Need the collection to be ordered since
            # the request key needs to be removed once its ordered
            self.requestQueues[ledger_id] = OrderedSet()

        self.batches = OrderedDict()  # type: OrderedDict[Tuple[int, int],
        # Tuple[int, float, bytes]]

        # TODO: Need to have a timer for each ledger
        self.lastBatchCreated = time.perf_counter()

        # self.lastOrderedPPSeqNo = 0
        # Three phase key for the last ordered batch
        self.last_ordered_3pc = (0, 0)

        # Keeps the `lastOrderedPPSeqNo` and ledger_summary for each view no.
        # GC when ordered last batch of the view
        self.view_ends_at = OrderedDict()

        # 3 phase key for the last prepared certificate before view change
        # started, applicable only to master instance
        self.last_prepared_before_view_change = None

    def ledger_uncommitted_size(self, ledgerId):
        if not self.isMaster:
            return None
        return self.node.getLedger(ledgerId).uncommitted_size

    def txnRootHash(self, ledgerId, toHex=True):
        if not self.isMaster:
            return None
        ledger = self.node.getLedger(ledgerId)
        h = ledger.uncommittedRootHash
        # If no uncommittedHash since this is the beginning of the tree
        # or no transactions affecting the ledger were made after the
        # last changes were committed
        root = h if h else ledger.tree.root_hash
        if toHex:
            root = hexlify(root).decode()
        return root

    def stateRootHash(self, ledgerId, toHex=True):
        if not self.isMaster:
            return None
        root = self.node.getState(ledgerId).headHash
        if toHex:
            root = hexlify(root).decode()
        return root

    @property
    def h(self) -> int:
        return self._h

    @h.setter
    def h(self, n):
        self._h = n
        self.H = self._h + self.config.LOG_SIZE
        logger.debug('{} set watermarks as {} {}'.format(self, self.h, self.H))

    @property
    def lastPrePrepareSeqNo(self):
        return self._lastPrePrepareSeqNo

    @lastPrePrepareSeqNo.setter
    def lastPrePrepareSeqNo(self, n):
        """
        This will _lastPrePrepareSeqNo to values greater than its previous
        values else it will not. To forcefully override as in case of `revert`,
        directly set `self._lastPrePrepareSeqNo`
        """
        if n > self._lastPrePrepareSeqNo:
            self._lastPrePrepareSeqNo = n
        else:
            logger.info('{} cannot set lastPrePrepareSeqNo to {} as its '
                         'already {}'.format(self, n, self._lastPrePrepareSeqNo))

    @property
    def requests(self):
        return self.node.requests

    @property
    def ledger_ids(self):
        return self.node.ledger_ids

    @staticmethod
    def generateName(nodeName: str, instId: int):
        """
        Create and return the name for a replica using its nodeName and
        instanceId.
         Ex: Alpha:1
        """
        return "{}:{}".format(nodeName, instId)

    @staticmethod
    def getNodeName(replicaName: str):
        return replicaName.split(":")[0]

    @property
    def isPrimary(self):
        """
        Is this node primary?

        :return: True if this node is primary, False if not, None if primary status not known
        """
        return self._primaryName == self.name if self._primaryName is not None \
            else None

    @property
    def hasPrimary(self):
        return self.primaryName is not None

    @property
    def primaryName(self):
        """
        Name of the primary replica of this replica's instance

        :return: Returns name if primary is known, None otherwise
        """
        return self._primaryName

    @primaryName.setter
    def primaryName(self, value: Optional[str]) -> None:
        """
        Set the value of isPrimary.

        :param value: the value to set isPrimary to
        """
        self.primaryNames[self.viewNo] = value
        if not value == self._primaryName:
            self._primaryName = value
            logger.debug("{} setting primaryName for view no {} to: {}".
                         format(self, self.viewNo, value))
            if self.isMaster:
                self.removeObsoletePpReqs()
            self._stateChanged()

    def primaryChanged(self, primaryName):
        self.primaryName = primaryName
        self._lastPrePrepareSeqNo = 0
        self.set_last_ordered_for_non_master()

    def shouldParticipate(self, viewNo: int, ppSeqNo: int) -> bool:
        """
        Replica should only participating in the consensus process and the
        replica did not stash any of this request's 3-phase request
        """
        return self.node.isParticipating and (viewNo, ppSeqNo) \
                                             not in self.stashingWhileCatchingUp

    def on_view_change_start(self):
        assert self.isMaster
        self.last_prepared_before_view_change = self.last_prepared_certificate_in_view(self.viewNo)
        logger.debug('{} setting last prepared for master to {}'.format(self, self.last_prepared_before_view_change))

    def on_view_change_done(self):
        assert self.isMaster
        self.last_prepared_before_view_change = None

    def get_lowest_probable_prepared_certificate_in_view(self, view_no) -> Optional[int]:
        """
        Return lowest pp_seq_no of the view for which can be prepared but
        choose from unprocessed PRE-PREPAREs and PREPAREs.
        """
        # TODO: Naive implementation, dont need to iterate over the complete
        # data structures, fix this later
        seq_no_pp = SortedList()      # pp_seq_no of PRE-PREPAREs
        # pp_seq_no of PREPAREs with count of PREPAREs for each
        seq_no_p = set()

        for (v, p) in self.prePreparesPendingPrevPP:
            if v == view_no:
                seq_no_pp.add(p)
            if v > view_no:
                break

        for (v, p), pr in self.preparesWaitingForPrePrepare.items():
            if v == view_no and len(pr) >= 2*self.f:
                seq_no_p.add(p)

        for n in seq_no_pp:
            if n in seq_no_p:
                return n
        return None

    def set_last_ordered_for_non_master(self):
        if not self.isMaster:
            # If not master instance choose last ordered seq no to be 1 less
            # the lowest prepared certificate in this view
            lowest_prepared = self.get_lowest_probable_prepared_certificate_in_view(
                self.viewNo)
            # TODO: This assumes some requests will be present, fix this once
            # view change is completely implemented
            lowest_ordered = 0 if lowest_prepared is None \
                else lowest_prepared - 1
            self.last_ordered_3pc = (self.viewNo, lowest_ordered)

    def removeObsoletePpReqs(self):
        # If replica was primary in previous view then remove every sent
        # Pre-Prepare with less than f+1 Prepares.
        viewNos = self.primaryNames.keys()
        if len(viewNos) > 1:
            viewNos = list(viewNos)
            lastViewNo = viewNos[-2]
            if self.primaryNames[lastViewNo] == self.name:
                lastViewPPs = []
                for (v, ps), pp in self.sentPrePrepares.items():
                    if v > lastViewNo:
                        break
                    if v == lastViewNo:
                        lastViewPPs.append(pp)

                obs = set()
                for pp in lastViewPPs:
                    if not self.prepares.hasEnoughVotes(pp, self.f):
                        obs.add((pp.viewNo, pp.ppSeqNo))

                for key in sorted(list(obs), key=itemgetter(1), reverse=True):
                    ppReq = self.sentPrePrepares[key]
                    count, _, prevStateRoot = self.batches.pop(key)
                    self.revert(ppReq.ledgerId, prevStateRoot, count)
                    self.sentPrePrepares.pop(key)
                    self.prepares.pop(key, None)

    # def revert_onordered_3pc_till(self, ordered_till: Tuple[int, int]):
    #     """
    #     Revert any changes to state and ledger that were not ordered by the
    #     replica but the replica got them through catchup
    #     """
    #     assert self.isMaster
    #     to_remove = []
    #     for key in reversed(self.batches):
    #         # TODO: Need to consider `self.ordered`
    #         if compare_3PC_keys(ordered_till, key) > 0:
    #             to_remove.append(key)
    #         else:
    #             break
    #
    #     for key in to_remove:
    #         ppReq = self.getPrePrepare(*key)
    #         count, _, prevStateRoot = self.batches.pop(key)
    #         self.revert(ppReq.ledgerId, prevStateRoot, count)
    #         # This GC should be done only once on view change complete
    #         # self.sentPrePrepares.pop(key, None)
    #         # self.prePrepares.pop(key, None)
    #         # self.prepares.pop(key, None)
    #         # self.prepares.pop(key, None)

    def revert_unordered_batches(self):
        to_remove = []
        for key in reversed(self.batches):
            if compare_3PC_keys(self.last_ordered_3pc, key) > 0:
                to_remove.append(key)
            else:
                break

        for key in to_remove:
            ppReq = self.getPrePrepare(*key)
            count, _, prevStateRoot = self.batches.pop(key)
            self.revert(ppReq.ledgerId, prevStateRoot, count)

        return len(to_remove)

    def is_primary_in_view(self, viewNo: int) -> Optional[bool]:
        """
        Return whether this replica was primary in the given view
        """
        return self.primaryNames[viewNo] == self.name

    def isMsgForCurrentView(self, msg):
        """
        Return whether this request's view number is equal to the current view
        number of this replica.
        """
        viewNo = getattr(msg, "viewNo", None)
        return viewNo == self.viewNo

    def isPrimaryForMsg(self, msg) -> Optional[bool]:
        """
        Return whether this replica is primary if the request's view number is
        equal this replica's view number and primary has been selected for
        the current view.
        Return None otherwise.
        :param msg: message
        """
        return self.isPrimary if self.isMsgForCurrentView(msg) \
            else self.is_primary_in_view(msg.viewNo)

    def isMsgFromPrimary(self, msg, sender: str) -> bool:
        """
        Return whether this message was from primary replica
        :param msg:
        :param sender:
        :return:
        """
        return self.primaryName == sender if self.isMsgForCurrentView(
            msg) else self.primaryNames[msg.viewNo] == sender

    def _stateChanged(self):
        """
        A series of actions to be performed when the state of this replica
        changes.

        - UnstashInBox (see _unstashInBox)
        """
        self._unstashInBox()
        if self.isPrimary is not None:
            try:
                self.processPostElectionMsgs()
            except SuspiciousNode as ex:
                self.outBox.append(ex)
                self.discard(ex.msg, ex.reason, logger.warning)

    def _stashInBox(self, msg):
        """
        Stash the specified message into the inBoxStash of this replica.

        :param msg: the message to stash
        """
        self.inBoxStash.append(msg)

    def _unstashInBox(self):
        """
        Append the inBoxStash to the right of the inBox.
        """
        # The stashed values need to go in "front" of the inBox.
        self.inBox.extendleft(self.inBoxStash)
        self.inBoxStash.clear()

    def __repr__(self):
        return self.name

    @property
    def f(self) -> int:
        """
        Return the number of Byzantine Failures that can be tolerated by this
        system. Equal to (N - 1)/3, where N is the number of nodes in the
        system.
        """
        return self.node.f

    @property
    def viewNo(self):
        """
        Return the current view number of this replica.
        """
        return self.node.viewNo

    def isMsgFromPrimary(self, msg, sender: str) -> bool:
        """
        Return whether this message was from primary replica
        :param msg:
        :param sender:
        :return:
        """
        return self.primaryName == sender

    def trackBatches(self, pp: PrePrepare, prevStateRootHash):
        # pp.discarded indicates the index from where the discarded requests
        #  starts hence the count of accepted requests, prevStateRoot is
        # tracked to revert this PRE-PREPARE
        logger.debug('{} tracking batch for {} with state root {}'.
                     format(self, pp, prevStateRootHash))
        self.batches[(pp.viewNo, pp.ppSeqNo)] = [pp.discarded, pp.ppTime,
                                                 prevStateRootHash]

    def send3PCBatch(self):
        r = 0
        for lid, q in self.requestQueues.items():
            if len(q) >= self.config.Max3PCBatchSize or (
                                self.lastBatchCreated +
                                self.config.Max3PCBatchWait <
                                time.perf_counter() and len(q) > 0):
                oldStateRootHash = self.stateRootHash(lid, toHex=False)
                ppReq = self.create3PCBatch(lid)
                self.sendPrePrepare(ppReq)
                self.trackBatches(ppReq, oldStateRootHash)
                r += 1

        if r > 0:
            self.lastBatchCreated = time.perf_counter()
        return r

    @staticmethod
    def batchDigest(reqs):
        return sha256(b''.join([r.digest.encode() for r in reqs])).hexdigest()

    def processReqDuringBatch(self, req: Request, validReqs: List,
                              inValidReqs: List, rejects: List):
        """
        This method will do dynamic validation and apply requests, also it
        will modify `validReqs`, `inValidReqs` and `rejects`
        """
        try:
            if self.isMaster:
                self.node.doDynamicValidation(req)
                self.node.applyReq(req)
        except (InvalidClientMessageException, UnknownIdentifier) as ex:
            logger.warning('{} encountered exception {} while processing {}, '
                            'will reject'.format(self, ex, req))
            rejects.append(Reject(req.identifier, req.reqId, ex))
            inValidReqs.append(req)
        else:
            validReqs.append(req)

    def create3PCBatch(self, ledger_id):
        ppSeqNo = self.lastPrePrepareSeqNo + 1
        logger.info("{} creating batch {} for ledger {} with state root {}".
                    format(self, ppSeqNo, ledger_id,
                           self.stateRootHash(ledger_id, toHex=False)))
        tm = time.time() * 1000
        validReqs = []
        inValidReqs = []
        rejects = []
        while len(validReqs)+len(inValidReqs) < self.config.Max3PCBatchSize \
                and self.requestQueues[ledger_id]:
            key = self.requestQueues[ledger_id].pop(0)  # Remove the first element
            fin_req = self.requests[key].finalised
            self.processReqDuringBatch(fin_req, validReqs, inValidReqs, rejects)

        reqs = validReqs+inValidReqs
        digest = self.batchDigest(reqs)
        prePrepareReq = PrePrepare(self.instId,
                                   self.viewNo,
                                   ppSeqNo,
                                   tm,
                                   [(req.identifier, req.reqId) for req in reqs],
                                   len(validReqs),
                                   digest,
                                   ledger_id,
                                   self.stateRootHash(ledger_id),
                                   self.txnRootHash(ledger_id)
                                   )
        logger.debug('{} created a PRE-PREPARE with {} requests for ledger {}'
                     .format(self, len(validReqs), ledger_id))
        self.lastPrePrepareSeqNo = ppSeqNo
        if self.isMaster:
            self.outBox.extend(rejects)
            self.node.onBatchCreated(ledger_id,
                                     self.stateRootHash(ledger_id, toHex=False))
        return prePrepareReq

    def sendPrePrepare(self, ppReq: PrePrepare):
        self.sentPrePrepares[ppReq.viewNo, ppReq.ppSeqNo] = ppReq
        self.send(ppReq, TPCStat.PrePrepareSent)

    def readyFor3PC(self, key: ReqKey):
        cls = self.node.__class__
        fin_req = self.requests[key].finalised
        self.requestQueues[cls.ledgerIdForRequest(fin_req)].add(key)

    def serviceQueues(self, limit=None):
        """
        Process `limit` number of messages in the inBox.

        :param limit: the maximum number of messages to process
        :return: the number of messages successfully processed
        """
        # TODO should handle SuspiciousNode here
        r = self.dequeuePrePrepares() if self.node.isParticipating else 0
        r += self.inBoxRouter.handleAllSync(self.inBox, limit)
        r += self.send3PCBatch() if (self.isPrimary and
                                     self.node.isParticipating) else 0
        r += self._serviceActions()
        return r
        # Messages that can be processed right now needs to be added back to the
        # queue. They might be able to be processed later

    def processPostElectionMsgs(self):
        """
        Process messages waiting for the election of a primary replica to
        complete.
        """
        while self.postElectionMsgs:
            msg = self.postElectionMsgs.popleft()
            logger.debug("{} processing pended msg {}".format(self, msg))
            self.dispatchThreePhaseMsg(*msg)

    @property
    def quorum(self) -> int:
        r"""
        Return the quorum of this RBFT system. Equal to :math:`2f + 1`.
        Return None if `f` is not yet determined.
        """
        return self.node.quorum

    def dispatchThreePhaseMsg(self, msg: ThreePhaseMsg, sender: str) -> Any:
        """
        Create a three phase request to be handled by the threePhaseRouter.

        :param msg: the ThreePhaseMsg to dispatch
        :param sender: the name of the node that sent this request
        """
        senderRep = self.generateName(sender, self.instId)
        if self.isPpSeqNoStable(msg.ppSeqNo):
            self.discard(msg,
                         "achieved stable checkpoint for 3 phase message",
                         logger.debug)
            return
        if self.isPpSeqNoBetweenWaterMarks(msg.ppSeqNo):
            try:
                if self.can_pp_seq_no_be_in_view(msg.viewNo, msg.ppSeqNo):
                    self.threePhaseRouter.handleSync((msg, senderRep))
                else:
                    self.discard(msg, 'un-acceptable pp seq no from previous '
                                      'view', logger.debug)
                    return
            except SuspiciousNode as ex:
                self.node.reportSuspiciousNodeEx(ex)
        else:
            logger.debug("{} stashing 3 phase message {} since ppSeqNo {} is "
                         "not between {} and {}".
                         format(self, msg, msg.ppSeqNo, self.h, self.H))
            self.stashOutsideWatermarks((msg, sender))

    def processThreePhaseMsg(self, msg: ThreePhaseMsg, sender: str):
        """
        Process a 3-phase (pre-prepare, prepare and commit) request.
        Dispatch the request only if primary has already been decided, otherwise
        stash it.

        :param msg: the Three Phase message, one of PRE-PREPARE, PREPARE,
            COMMIT
        :param sender: name of the node that sent this message
        """
        if self.isPrimary is None:
            self.postElectionMsgs.append((msg, sender))
            logger.debug("Replica {} pended request {} from {}".
                         format(self, msg, sender))
            return
        self.dispatchThreePhaseMsg(msg, sender)

    def processPrePrepare(self, pp: PrePrepare, sender: str):
        """
        Validate and process the PRE-PREPARE specified.
        If validation is successful, create a PREPARE and broadcast it.

        :param pp: a prePrepareRequest
        :param sender: name of the node that sent this message
        """
        key = (pp.viewNo, pp.ppSeqNo)
        logger.debug("{} received PRE-PREPARE{} from {} at {}".
                     format(self, key, sender, time.perf_counter()))
        # Converting each req_idrs from list to tuple
        pp = updateNamedTuple(pp, **{f.REQ_IDR.nm: [(i, r)
                                                    for i, r in pp.reqIdr]})
        oldStateRoot = self.stateRootHash(pp.ledgerId, toHex=False)
        if self.canProcessPrePrepare(pp, sender):
            self.addToPrePrepares(pp)
            if not self.node.isParticipating:
                self.stashingWhileCatchingUp.add(key)
                logger.debug('{} stashing PRE-PREPARE{}'.format(self, key))
                return

            if self.isMaster:
                self.node.onBatchCreated(pp.ledgerId,
                                         self.stateRootHash(pp.ledgerId,
                                                            toHex=False))
            self.trackBatches(pp, oldStateRoot)
            logger.debug("{} processed incoming PRE-PREPARE{}".format(self, key),
                         extra={"tags": ["processing"]})

    def tryPrepare(self, pp: PrePrepare):
        """
        Try to send the Prepare message if the PrePrepare message is ready to
        be passed into the Prepare phase.
        """
        rv, msg = self.canPrepare(pp)
        if rv:
            self.doPrepare(pp)
        else:
            logger.debug("{} cannot send PREPARE since {}".format(self, msg))

    def processPrepare(self, prepare: Prepare, sender: str) -> None:
        """
        Validate and process the PREPARE specified.
        If validation is successful, create a COMMIT and broadcast it.

        :param prepare: a PREPARE msg
        :param sender: name of the node that sent the PREPARE
        """
        # TODO move this try/except up higher
        logger.debug("{} received PREPARE{} from {}".
                     format(self, (prepare.viewNo, prepare.ppSeqNo), sender))
        if self.isPpSeqNoStable(prepare.ppSeqNo):
            self.discard(prepare,
                         "achieved stable checkpoint for Preapre",
                         logger.debug)
            return
        try:
            if self.validatePrepare(prepare, sender):
                self.addToPrepares(prepare, sender)
                self.stats.inc(TPCStat.PrepareRcvd)
                logger.debug("{} processed incoming PREPARE {}".
                             format(self, (prepare.viewNo, prepare.ppSeqNo)))
            else:
                # TODO let's have isValidPrepare throw an exception that gets
                # handled and possibly logged higher
                logger.debug("{} cannot process incoming PREPARE".
                               format(self))
        except SuspiciousNode as ex:
            self.node.reportSuspiciousNodeEx(ex)

    def processCommit(self, commit: Commit, sender: str) -> None:
        """
        Validate and process the COMMIT specified.
        If validation is successful, return the message to the node.

        :param commit: an incoming COMMIT message
        :param sender: name of the node that sent the COMMIT
        """
        logger.debug("{} received COMMIT{} from {}".
                     format(self, (commit.viewNo, commit.ppSeqNo), sender))
        if self.isPpSeqNoStable(commit.ppSeqNo):
            self.discard(commit,
                         "achieved stable checkpoint for Commit",
                         logger.debug)
            return

        if self.validateCommit(commit, sender):
            self.stats.inc(TPCStat.CommitRcvd)
            self.addToCommits(commit, sender)
            logger.debug("{} processed incoming COMMIT{}".
                         format(self, (commit.viewNo, commit.ppSeqNo)))

    def tryCommit(self, prepare: Prepare):
        """
        Try to commit if the Prepare message is ready to be passed into the
        commit phase.
        """
        rv, reason = self.canCommit(prepare)
        if rv:
            self.doCommit(prepare)
        else:
            logger.debug("{} cannot send COMMIT since {}".
                         format(self, reason))

    def tryOrder(self, commit: Commit):
        """
        Try to order if the Commit message is ready to be ordered.
        """
        canOrder, reason = self.canOrder(commit)
        if canOrder:
            logger.trace("{} returning request to node".format(self))
            self.doOrder(commit)
        else:
            logger.debug("{} cannot return request to node: {}".
                         format(self, reason))
        return canOrder

    def doPrepare(self, pp: PrePrepare):
        logger.debug("{} Sending PREPARE {} at {}".
                     format(self, (pp.viewNo, pp.ppSeqNo), time.perf_counter()))
        prepare = Prepare(self.instId,
                          pp.viewNo,
                          pp.ppSeqNo,
                          pp.digest,
                          pp.stateRootHash,
                          pp.txnRootHash
                          )
        self.send(prepare, TPCStat.PrepareSent)
        self.addToPrepares(prepare, self.name)

    def doCommit(self, p: Prepare):
        """
        Create a commit message from the given Prepare message and trigger the
        commit phase
        :param p: the prepare message
        """
        logger.debug("{} Sending COMMIT{} at {}".
                     format(self, (p.viewNo, p.ppSeqNo), time.perf_counter()))
        commit = Commit(self.instId,
                        p.viewNo,
                        p.ppSeqNo)
        self.send(commit, TPCStat.CommitSent)
        self.addToCommits(commit, self.name)

    def nonFinalisedReqs(self, reqKeys: List[Tuple[str, int]]):
        """
        Check if there are any requests which are not finalised, i.e for
        which there are not enough PROPAGATEs
        """
        return {key for key in reqKeys if not self.requests.isFinalised(key)}

    def isNextPrePrepare(self, view_no: int, pp_seq_no: int):
        """

        """
        if view_no == self.viewNo and pp_seq_no == 1:
            # First PRE-PREPARE in a new view
            return True

        last_pp = self.lastPrePrepare
        if last_pp:
            if last_pp.viewNo == view_no:
                if last_pp.viewNo == self.last_ordered_3pc[0] and \
                                last_pp.ppSeqNo < self.last_ordered_3pc[1]:
                    last_pp_seq_no = self.last_ordered_3pc[1]
                else:
                    last_pp_seq_no = last_pp.ppSeqNo
            elif last_pp.viewNo > view_no:
                return False
            else:
                assert view_no == self.viewNo
                last_pp_seq_no = 0
        else:
            # No PRE-PREPARE found, maybe the node just started.
            if view_no == self.last_ordered_3pc[0]:
                last_pp_seq_no = self.last_ordered_3pc[1]
            else:
                return False

        if pp_seq_no - last_pp_seq_no != 1:
            logger.debug('{} missing PRE-PREPAREs between {} and {}'.
                         format(self, pp_seq_no, last_pp_seq_no))
            # TODO: think of a better way, urgently
            self.set_last_ordered_for_non_master()
            return False
        return True

    def revert(self, ledgerId, stateRootHash, reqCount):
        # A batch should only be reverted if all batches that came after it
        # have been reverted
        ledger = self.node.getLedger(ledgerId)
        state = self.node.getState(ledgerId)
        logger.info('{} reverting {} txns and state root from {} to {} for'
                    ' ledger {}'.format(self, reqCount, state.headHash,
                                        stateRootHash, ledgerId))
        state.revertToHead(stateRootHash)
        ledger.discardTxns(reqCount)
        self.node.onBatchRejected(ledgerId)

    def validatePrePrepare(self, pp: PrePrepare, sender: str):
        """
        This will apply the requests part of the PrePrepare to the ledger
        and state. It will not commit though (the ledger on disk will not
        change, neither the committed state root hash will change)
        """
        validReqs = []
        inValidReqs = []
        rejects = []
        if self.isMaster:
            # If this PRE-PREPARE is not valid then state and ledger should be
            # reverted
            oldStateRoot = self.stateRootHash(pp.ledgerId, toHex=False)
            logger.debug('{} state root before processing {} is {}'.
                         format(self, pp, oldStateRoot))

        for reqKey in pp.reqIdr:
            req = self.requests[reqKey].finalised
            self.processReqDuringBatch(req, validReqs, inValidReqs, rejects)

        if len(validReqs) != pp.discarded:
            if self.isMaster:
                self.revert(pp.ledgerId, oldStateRoot, len(validReqs))
            raise SuspiciousNode(sender, Suspicions.PPR_REJECT_WRONG, pp)

        reqs = validReqs + inValidReqs
        digest = self.batchDigest(reqs)

        # A PRE-PREPARE is sent that does not match request digest
        if digest != pp.digest:
            if self.isMaster:
                self.revert(pp.ledgerId, oldStateRoot, len(validReqs))
            raise SuspiciousNode(sender, Suspicions.PPR_DIGEST_WRONG, pp)

        if self.isMaster:
            if pp.stateRootHash != self.stateRootHash(pp.ledgerId):
                self.revert(pp.ledgerId, oldStateRoot, len(validReqs))
                raise SuspiciousNode(sender, Suspicions.PPR_STATE_WRONG, pp)

            if pp.txnRootHash != self.txnRootHash(pp.ledgerId):
                self.revert(pp.ledgerId, oldStateRoot, len(validReqs))
                raise SuspiciousNode(sender, Suspicions.PPR_TXN_WRONG, pp)

            self.outBox.extend(rejects)

    def canProcessPrePrepare(self, pp: PrePrepare, sender: str) -> bool:
        """
        Decide whether this replica is eligible to process a PRE-PREPARE,
        based on the following criteria:

        - this replica is non-primary replica
        - the request isn't in its list of received PRE-PREPAREs
        - the request is waiting to for PRE-PREPARE and the digest value matches

        :param pp: a PRE-PREPARE msg to process
        :param sender: the name of the node that sent the PRE-PREPARE msg
        :return: True if processing is allowed, False otherwise
        """
        # TODO: Check whether it is rejecting PRE-PREPARE from previous view
        # PRE-PREPARE should not be sent from non primary
        if not self.isMsgFromPrimary(pp, sender):
            raise SuspiciousNode(sender, Suspicions.PPR_FRM_NON_PRIMARY, pp)

        # A PRE-PREPARE is being sent to primary
        if self.isPrimaryForMsg(pp) is True:
            raise SuspiciousNode(sender, Suspicions.PPR_TO_PRIMARY, pp)

        # A PRE-PREPARE is sent that has already been received
        if (pp.viewNo, pp.ppSeqNo) in self.prePrepares:
            raise SuspiciousNode(sender, Suspicions.DUPLICATE_PPR_SENT, pp)

        if not self.node.isParticipating:
            # Let the node stash the pre-prepare
            # TODO: The next processed pre-prepare needs to take consider if
            # the last pre-prepare was stashed or not since stashed requests
            # do not make change to state or ledger
            return True

        nonFinReqs = self.nonFinalisedReqs(pp.reqIdr)

        if nonFinReqs:
            self.enqueuePrePrepare(pp, sender, nonFinReqs)
            return False

        if not self.isNextPrePrepare(pp.viewNo, pp.ppSeqNo):
            self.enqueuePrePrepare(pp, sender)
            return False

        self.validatePrePrepare(pp, sender)

        return True

    def addToPrePrepares(self, pp: PrePrepare) -> None:
        """
        Add the specified PRE-PREPARE to this replica's list of received
        PRE-PREPAREs and try sending PREPARE

        :param pp: the PRE-PREPARE to add to the list
        """
        key = (pp.viewNo, pp.ppSeqNo)
        self.prePrepares[key] = pp
        self.lastPrePrepareSeqNo = pp.ppSeqNo
        self.dequeuePrepares(*key)
        self.dequeueCommits(*key)
        self.stats.inc(TPCStat.PrePrepareRcvd)
        self.tryPrepare(pp)

    def hasPrepared(self, request) -> bool:
        return self.prepares.hasPrepareFrom(request, self.name)

    def canPrepare(self, ppReq) -> (bool, str):
        """
        Return whether the batch of requests in the PRE-PREPARE can
        proceed to the PREPARE step.

        :param ppReq: any object with identifier and requestId attributes
        """
        if not self.shouldParticipate(ppReq.viewNo, ppReq.ppSeqNo):
            return False, 'should not participate in consensus for {}'.format(ppReq)
        if self.hasPrepared(ppReq):
            return False, 'has already sent PREPARE for {}'.format(ppReq)
        return True, ''

    def validatePrepare(self, prepare: Prepare, sender: str) -> bool:
        """
        Return whether the PREPARE specified is valid.

        :param prepare: the PREPARE to validate
        :param sender: the name of the node that sent the PREPARE
        :return: True if PREPARE is valid, False otherwise
        """
        key = (prepare.viewNo, prepare.ppSeqNo)
        primaryStatus = self.isPrimaryForMsg(prepare)

        ppReq = self.getPrePrepare(*key)

        # If a non primary replica and receiving a PREPARE request before a
        # PRE-PREPARE request, then proceed

        # PREPARE should not be sent from primary
        if self.isMsgFromPrimary(prepare, sender):
            raise SuspiciousNode(sender, Suspicions.PR_FRM_PRIMARY, prepare)

        # If non primary replica
        if primaryStatus is False:
            if self.prepares.hasPrepareFrom(prepare, sender):
                raise SuspiciousNode(sender, Suspicions.DUPLICATE_PR_SENT, prepare)
            # If PRE-PREPARE not received for the PREPARE, might be slow network
            if not ppReq:
                self.enqueuePrepare(prepare, sender)
                return False
        # If primary replica
        if primaryStatus is True:
            if self.prepares.hasPrepareFrom(prepare, sender):
                raise SuspiciousNode(sender, Suspicions.DUPLICATE_PR_SENT, prepare)
            # If PRE-PREPARE was not sent for this PREPARE, certainly
            # malicious behavior
            elif not ppReq:
                raise SuspiciousNode(sender, Suspicions.UNKNOWN_PR_SENT, prepare)

        if primaryStatus is None and not ppReq:
            self.enqueuePrepare(prepare, sender)
            return False

        if prepare.digest != ppReq.digest:
            raise SuspiciousNode(sender, Suspicions.PR_DIGEST_WRONG, prepare)

        elif prepare.stateRootHash != ppReq.stateRootHash:
            raise SuspiciousNode(sender, Suspicions.PR_STATE_WRONG,
                                 prepare)
        elif prepare.txnRootHash != ppReq.txnRootHash:
            raise SuspiciousNode(sender, Suspicions.PR_TXN_WRONG,
                                 prepare)
        else:
            return True

    def addToPrepares(self, prepare: Prepare, sender: str):
        """
        Add the specified PREPARE to this replica's list of received
        PREPAREs and try sending COMMIT

        :param prepare: the PREPARE to add to the list
        """
        self.prepares.addVote(prepare, sender)
        self.tryCommit(prepare)

    def getPrePrepare(self, viewNo, ppSeqNo):
        key = (viewNo, ppSeqNo)
        if key in self.sentPrePrepares:
            return self.sentPrePrepares[key]
        if key in self.prePrepares:
            return self.prePrepares[key]

    @property
    def lastPrePrepare(self):
        last_3pc = (0, 0)
        lastPp = None
        if self.sentPrePrepares:
            (v, s), pp = self.peekitem(self.sentPrePrepares, -1)
            last_3pc = (v, s)
            lastPp = pp
        if self.prePrepares:
            (v, s), pp = self.peekitem(self.prePrepares, -1)
            if compare_3PC_keys(last_3pc, (v, s)) > 0:
                lastPp = pp
        return lastPp

    def hasCommitted(self, request) -> bool:
        return self.commits.hasCommitFrom(ThreePhaseKey(
            request.viewNo, request.ppSeqNo), self.name)

    def canCommit(self, prepare: Prepare) -> (bool, str):
        """
        Return whether the specified PREPARE can proceed to the Commit
        step.

        Decision criteria:

        - If this replica has got just 2f PREPARE requests then commit request.
        - If less than 2f PREPARE requests then probably there's no consensus on
            the request; don't commit
        - If more than 2f then already sent COMMIT; don't commit

        :param prepare: the PREPARE
        """
        if not self.shouldParticipate(prepare.viewNo, prepare.ppSeqNo):
            return False, 'should not participate in consensus for {}'.format(prepare)
        if not self.prepares.hasQuorum(prepare, self.f):
            return False, 'does not have prepare quorum for {}'.format(prepare)
        if self.hasCommitted(prepare):
            return False, 'has already sent COMMIT for {}'.format(prepare)
        return True, ''

    def validateCommit(self, commit: Commit, sender: str) -> bool:
        """
        Return whether the COMMIT specified is valid.

        :param commit: the COMMIT to validate
        :return: True if `request` is valid, False otherwise
        """
        key = (commit.viewNo, commit.ppSeqNo)
        ppReq = self.getPrePrepare(*key)
        if not ppReq:
            self.enqueueCommit(commit, sender)
            return False

        if (key not in self.prepares and
                key not in self.preparesWaitingForPrePrepare):
            logger.debug("{} rejecting COMMIT{} due to lack of prepares".
                         format(self, key))
            # raise SuspiciousNode(sender, Suspicions.UNKNOWN_CM_SENT, commit)
            return False
        elif self.commits.hasCommitFrom(commit, sender):
            raise SuspiciousNode(sender, Suspicions.DUPLICATE_CM_SENT, commit)
        else:
            return True

    def addToCommits(self, commit: Commit, sender: str):
        """
        Add the specified COMMIT to this replica's list of received
        commit requests.

        :param commit: the COMMIT to add to the list
        :param sender: the name of the node that sent the COMMIT
        """
        self.commits.addVote(commit, sender)
        self.tryOrder(commit)

    def hasOrdered(self, viewNo, ppSeqNo) -> bool:
        return (viewNo, ppSeqNo) in self.ordered

    def canOrder(self, commit: Commit) -> Tuple[bool, Optional[str]]:
        """
        Return whether the specified commitRequest can be returned to the node.

        Decision criteria:

        - If have got just 2f+1 Commit requests then return request to node
        - If less than 2f+1 of commit requests then probably don't have
            consensus on the request; don't return request to node
        - If more than 2f+1 then already returned to node; don't return request
            to node

        :param commit: the COMMIT
        """
        if not self.commits.hasQuorum(commit, self.f):
            return False, "no quorum: {} commits where f is {}".\
                          format(commit, self.f)

        key = (commit.viewNo, commit.ppSeqNo)
        if self.hasOrdered(*key):
            return False, "already ordered"

        if commit.ppSeqNo > 1 and not self.all_prev_ordered(commit):
            viewNo, ppSeqNo = commit.viewNo, commit.ppSeqNo
            if viewNo not in self.stashed_out_of_order_commits:
                self.stashed_out_of_order_commits[viewNo] = {}
            self.stashed_out_of_order_commits[viewNo][ppSeqNo] = commit
            self.startRepeating(self.process_stashed_out_of_order_commits, 1)
            return False, "stashing {} since out of order".\
                format(commit)

        return True, None

    def all_prev_ordered(self, commit: Commit):
        """
        Return True if all previous COMMITs have been ordered
        """
        # TODO: This method does a lot of work, choose correct data
        # structures to make it efficient.

        viewNo, ppSeqNo = commit.viewNo, commit.ppSeqNo

        if self.ordered and self.ordered[-1] == (viewNo, ppSeqNo-1):
            # Last ordered was in same view as this COMMIT
            return True

        # if some PREPAREs/COMMITs were completely missed in the same view
        toCheck = set()
        toCheck.update(set(self.sentPrePrepares.keys()))
        toCheck.update(set(self.prePrepares.keys()))
        toCheck.update(set(self.prepares.keys()))
        toCheck.update(set(self.commits.keys()))
        for (v, p) in toCheck:
            if v < viewNo and (v, p) not in self.ordered:
                # Have commits from previous view that are unordered.
                return False
            if v == viewNo and p < ppSeqNo and (v, p) not in self.ordered:
                # If unordered commits are found with lower ppSeqNo then this
                # cannot be ordered.
                return False

        return True

    def process_stashed_out_of_order_commits(self):
        # This method is called periodically to check for any commits that
        # were stashed due to lack of commits before them and orders them if it can
        logger.debug('{} trying to order from out of order commits. {} {}'.
                     format(self, self.ordered, self.stashed_out_of_order_commits))
        if self.ordered:
            lastOrdered = self.ordered[-1]
            vToRemove = set()
            for v in self.stashed_out_of_order_commits:
                if v < lastOrdered[0] and self.stashed_out_of_order_commits[v]:
                    raise RuntimeError("{} found commits {} from previous view {}"
                                       " that were not ordered but last ordered"
                                       " is {}".format(self, self.stashed_out_of_order_commits[v], v, lastOrdered))
                pToRemove = set()
                for p, commit in self.stashed_out_of_order_commits[v].items():
                    if (v, p) in self.ordered:
                        pToRemove.add(p)
                        continue
                    if (v == lastOrdered[0] and lastOrdered == (v, p - 1)) or \
                            (v > lastOrdered[0] and self.isLowestCommitInView(commit)):
                        logger.debug("{} ordering stashed commit {}".
                                     format(self, commit))
                        if self.tryOrder(commit):
                            lastOrdered = (v, p)
                            pToRemove.add(p)

                for p in pToRemove:
                    del self.stashed_out_of_order_commits[v][p]
                if not self.stashed_out_of_order_commits[v]:
                    vToRemove.add(v)

            for v in vToRemove:
                del self.stashed_out_of_order_commits[v]

            if not self.stashed_out_of_order_commits:
                self.stopRepeating(self.process_stashed_out_of_order_commits)

    def isLowestCommitInView(self, commit):
        view_no = commit.viewNo
        if view_no > self.viewNo:
            logger.debug('{} encountered {} which belongs to a later view'
                         .format(self, commit))
            return False
        # if view_no != self.viewNo and view_no not in self.view_ends_at:
        #     logger.debug('{} encountered {} from past view for which dont know '
        #                  'the end of view'.format(self, commit))
        #     return False
        #
        # ppSeqNos = []
        # for v, p in self.commits:
        #     if v == commit.viewNo:
        #         ppSeqNos.append(p)
        # return min(ppSeqNos) == commit.ppSeqNo if ppSeqNos else True
        return commit.ppSeqNo == 1

    def last_prepared_certificate_in_view(self, view_no) -> Optional[Tuple[int, int]]:
        # Pick the latest sent COMMIT in the view.
        # TODO: Consider stashed messages too?
        assert self.isMaster
        return max_3PC_key(self.commits.keys()) if self.commits else None

    def doOrder(self, commit: Commit):
        key = (commit.viewNo, commit.ppSeqNo)
        logger.debug("{} ordering COMMIT{}".format(self, key))
        return self.order_3pc_key(key)

    def order_3pc_key(self, key):
        pp = self.getPrePrepare(*key)
        assert pp
        self.addToOrdered(*key)
        ordered = Ordered(self.instId,
                          pp.viewNo,
                          pp.reqIdr[:pp.discarded],
                          pp.ppSeqNo,
                          pp.ppTime,
                          pp.ledgerId,
                          pp.stateRootHash,
                          pp.txnRootHash)
        # TODO: Should not order or add to checkpoint while syncing
        # 3 phase state.
        if key in self.stashingWhileCatchingUp:
            if self.isMaster and self.node.isParticipating:
                # While this request arrived the node was catching up but the
                # node has caught up and applied the stash so apply this request
                logger.debug('{} found that 3PC of ppSeqNo {} outlived the '
                             'catchup process'.format(self, pp.ppSeqNo))
                for reqKey in pp.reqIdr[:pp.discarded]:
                    req = self.requests[reqKey].finalised
                    self.node.applyReq(req)
            self.stashingWhileCatchingUp.remove(key)

        for k in pp.reqIdr:
            # Using discard since the key may not be present as in case of
            # primary, the key was popped out while creating PRE-PREPARE.
            # Or in case of node catching up, it will not validate
            # PRE-PREPAREs or PREPAREs but will only validate number of COMMITs
            #  and their consistency with PRE-PREPARE of PREPAREs
            self.requestQueues[pp.ledgerId].discard(k)

        self.send(ordered, TPCStat.OrderSent)
        logger.debug("{} ordered request {}".format(self, key))
        self.addToCheckpoint(pp.ppSeqNo, pp.digest)
        return True

    def processCheckpoint(self, msg: Checkpoint, sender: str):
        logger.debug('{} received checkpoint {} from {}'.
                     format(self, msg, sender))
        seqNoEnd = msg.seqNoEnd
        if self.isPpSeqNoStable(seqNoEnd):
            self.discard(msg, reason="Checkpoint already stable",
                         logMethod=logger.debug)
            return

        seqNoStart = msg.seqNoStart
        key = (seqNoStart, seqNoEnd)
        if key in self.checkpoints and self.checkpoints[key].digest:
            ckState = self.checkpoints[key]
            if ckState.digest == msg.digest:
                ckState.receivedDigests[sender] = msg.digest
            else:
                logger.error("{} received an incorrect digest {} for "
                             "checkpoint {} from {}".format(self,
                                                            msg.digest,
                                                            key,
                                                            sender))
                return
            self.checkIfCheckpointStable(key)
        else:
            self.stashCheckpoint(msg, sender)

    def _newCheckpointState(self, ppSeqNo, digest) -> CheckpointState:
        s, e = ppSeqNo, ppSeqNo + self.config.CHK_FREQ - 1
        logger.debug("{} adding new checkpoint state for {}".
                     format(self, (s, e)))
        state = CheckpointState(ppSeqNo, [digest, ], None, {}, False)
        self.checkpoints[s, e] = state
        return state

    def addToCheckpoint(self, ppSeqNo, digest):
        for (s, e) in self.checkpoints.keys():
            if s <= ppSeqNo <= e:
                state = self.checkpoints[s, e]  # type: CheckpointState
                state.digests.append(digest)
                state = updateNamedTuple(state, seqNo=ppSeqNo)
                self.checkpoints[s, e] = state
                break
        else:
            state = self._newCheckpointState(ppSeqNo, digest)
            s, e = ppSeqNo, ppSeqNo + self.config.CHK_FREQ - 1

        if len(state.digests) == self.config.CHK_FREQ:
            state = updateNamedTuple(state,
                                     digest=sha256(
                                         serialize(state.digests).encode()
                                     ).hexdigest(),
                                     digests=[])
            self.checkpoints[s, e] = state
            self.send(Checkpoint(self.instId, self.viewNo, s, e,
                                 state.digest))
            self.processStashedCheckpoints((s, e))

    def markCheckPointStable(self, seqNo):
        previousCheckpoints = []
        for (s, e), state in self.checkpoints.items():
            if e == seqNo:
                state = updateNamedTuple(state, isStable=True)
                self.checkpoints[s, e] = state
                break
            else:
                previousCheckpoints.append((s, e))
        else:
            logger.error("{} could not find {} in checkpoints".
                         format(self, seqNo))
            return
        self.h = seqNo
        for k in previousCheckpoints:
            logger.debug("{} removing previous checkpoint {}".format(self, k))
            self.checkpoints.pop(k)
        self.gc(seqNo)
        logger.debug("{} marked stable checkpoint {}".format(self, (s, e)))
        self.processStashedMsgsForNewWaterMarks()

    def checkIfCheckpointStable(self, key: Tuple[int, int]):
        ckState = self.checkpoints[key]
        if len(ckState.receivedDigests) == 2 * self.f:
            self.markCheckPointStable(ckState.seqNo)
            return True
        else:
            logger.debug('{} has state.receivedDigests as {}'.
                         format(self, ckState.receivedDigests.keys()))
            return False

    def stashCheckpoint(self, ck: Checkpoint, sender: str):
        seqNoStart, seqNoEnd = ck.seqNoStart, ck.seqNoEnd
        if (seqNoStart, seqNoEnd) not in self.stashedRecvdCheckpoints:
            self.stashedRecvdCheckpoints[seqNoStart, seqNoEnd] = {}
        self.stashedRecvdCheckpoints[seqNoStart, seqNoEnd][sender] = ck

    def processStashedCheckpoints(self, key):
        i = 0
        if key in self.stashedRecvdCheckpoints:
            for sender, ck in self.stashedRecvdCheckpoints[key].items():
                self.processCheckpoint(ck, sender)
                i += 1
        logger.debug('{} processed {} stashed checkpoints for {}'.
                     format(self, i, key))
        return i

    def gc(self, tillSeqNo):
        logger.debug("{} cleaning up till {}".format(self, tillSeqNo))
        tpcKeys = set()
        reqKeys = set()
        for (v, p), pp in self.sentPrePrepares.items():
            if p <= tillSeqNo:
                tpcKeys.add((v, p))
                for reqKey in pp.reqIdr:
                    reqKeys.add(reqKey)
        for (v, p), pp in self.prePrepares.items():
            if p <= tillSeqNo:
                tpcKeys.add((v, p))
                for reqKey in pp.reqIdr:
                    reqKeys.add(reqKey)

        logger.debug("{} found {} 3 phase keys to clean".
                     format(self, len(tpcKeys)))
        logger.debug("{} found {} request keys to clean".
                     format(self, len(reqKeys)))

        for k in tpcKeys:
            self.sentPrePrepares.pop(k, None)
            self.prePrepares.pop(k, None)
            self.prepares.pop(k, None)
            self.commits.pop(k, None)

        for k in reqKeys:
            self.requests[k].forwardedTo -= 1
            if self.requests[k].forwardedTo == 0:
                logger.debug('{} clearing requests {} from previous checkpoints'.
                             format(self, len(reqKeys)))
                self.requests.pop(k)

    def stashOutsideWatermarks(self, item: Union[ReqDigest, Tuple]):
        self.stashingWhileOutsideWaterMarks.append(item)

    def processStashedMsgsForNewWaterMarks(self):
        # `stashingWhileOutsideWaterMarks` can grow from methods called in the
        # loop below, so `stashingWhileOutsideWaterMarks` might never
        # become empty during the execution of this method resulting
        # in an infinite loop
        itemsToConsume = len(self.stashingWhileOutsideWaterMarks)
        while itemsToConsume:
            item = self.stashingWhileOutsideWaterMarks.popleft()
            logger.debug("{} processing stashed item {} after new stable "
                         "checkpoint".format(self, item))

            if isinstance(item, tuple) and len(item) == 2:
                self.dispatchThreePhaseMsg(*item)
            else:
                logger.error("{} cannot process {} "
                             "from stashingWhileOutsideWaterMarks".
                             format(self, item))
            itemsToConsume -= 1

    @staticmethod
    def peekitem(d, i):
        # Adding it since its not present in version supported by
        # Ubuntu repositories.
        key = d._list[i]
        return key, d[key]

    @property
    def firstCheckPoint(self) -> Tuple[Tuple[int, int], CheckpointState]:
        if not self.checkpoints:
            return None
        else:
            return self.peekitem(self.checkpoints, 0)
            # return self.checkpoints.peekitem(0)

    @property
    def lastCheckPoint(self) -> Tuple[Tuple[int, int], CheckpointState]:
        if not self.checkpoints:
            return None
        else:
            return self.peekitem(self.checkpoints, -1)
            # return self.checkpoints.peekitem(-1)

    def isPpSeqNoStable(self, ppSeqNo):
        """
        :param ppSeqNo:
        :return: True if ppSeqNo is less than or equal to last stable
        checkpoint, false otherwise
        """
        ck = self.firstCheckPoint
        if ck:
            _, ckState = ck
            return ckState.isStable and ckState.seqNo >= ppSeqNo
        else:
            return False

    def isPpSeqNoBetweenWaterMarks(self, ppSeqNo: int):
        return self.h < ppSeqNo <= self.H

    def addToOrdered(self, viewNo: int, ppSeqNo: int):
        self.ordered.add((viewNo, ppSeqNo))
        self.last_ordered_3pc = (viewNo, ppSeqNo)

    def enqueuePrePrepare(self, ppMsg: PrePrepare, sender: str,
                          nonFinReqs: Set=None):
        if nonFinReqs:
            logger.debug("Queueing pre-prepares due to unavailability of finalised "
                         "requests. PrePrepare {} from {}".format(ppMsg, sender))
            self.prePreparesPendingFinReqs.append((ppMsg, sender, nonFinReqs))
        else:
            # Possible exploit, an malicious party can send an invalid
            # pre-prepare and over-write the correct one?
            logger.debug(
                "Queueing pre-prepares due to unavailability of previous "
                "pre-prepares. PrePrepare {} from {}".format(ppMsg, sender))
            self.prePreparesPendingPrevPP[ppMsg.viewNo, ppMsg.ppSeqNo] = (ppMsg, sender)

    def dequeuePrePrepares(self):
        """
        Dequeue any received PRE-PREPAREs that did not have finalized requests
        or the replica was missing any PRE-PREPAREs before it
        :return:
        """
        ppsReady = []
        # Check if any requests have become finalised belonging to any stashed
        # PRE-PREPAREs.
        for i, (pp, sender, reqIds) in enumerate(self.prePreparesPendingFinReqs):
            finalised = set()
            for r in reqIds:
                if self.requests.isFinalised(r):
                    finalised.add(r)
            diff = reqIds.difference(finalised)
            # All requests become finalised
            if not diff:
                ppsReady.append(i)
            self.prePreparesPendingFinReqs[i] = (pp, sender, diff)

        for i in sorted(ppsReady, reverse=True):
            pp, sender, _ = self.prePreparesPendingFinReqs.pop(i)
            self.prePreparesPendingPrevPP[pp.viewNo, pp.ppSeqNo] = (pp, sender)

        r = 0
        while self.prePreparesPendingPrevPP and self.isNextPrePrepare(
                *self.prePreparesPendingPrevPP.iloc[0]):
            _, (pp, sender) = self.prePreparesPendingPrevPP.popitem(last=False)
            if not self.can_pp_seq_no_be_in_view(pp.viewNo, pp.ppSeqNo):
                self.discard(pp, "Pre-Prepare from a previous view",
                             logger.debug)
                continue
            self.processPrePrepare(pp, sender)
            r += 1
        return r

    def enqueuePrepare(self, pMsg: Prepare, sender: str):
        logger.debug("Queueing prepare due to unavailability of PRE-PREPARE. "
                     "Prepare {} from {}".format(pMsg, sender))
        key = (pMsg.viewNo, pMsg.ppSeqNo)
        if key not in self.preparesWaitingForPrePrepare:
            self.preparesWaitingForPrePrepare[key] = deque()
        self.preparesWaitingForPrePrepare[key].append((pMsg, sender))

    def dequeuePrepares(self, viewNo: int, ppSeqNo: int):
        key = (viewNo, ppSeqNo)
        if key in self.preparesWaitingForPrePrepare:
            i = 0
            # Keys of pending prepares that will be processed below
            while self.preparesWaitingForPrePrepare[key]:
                prepare, sender = self.preparesWaitingForPrePrepare[
                    key].popleft()
                logger.debug("{} popping stashed PREPARE{}".format(self, key))
                self.processPrepare(prepare, sender)
                i += 1
            self.preparesWaitingForPrePrepare.pop(key)
            logger.debug("{} processed {} PREPAREs waiting for PRE-PREPARE for"
                         " view no {} and seq no {}".
                         format(self, i, viewNo, ppSeqNo))

    def enqueueCommit(self, request: Commit, sender: str):
        logger.debug("Queueing commit due to unavailability of PREPARE. "
                     "Request {} from {}".format(request, sender))
        key = (request.viewNo, request.ppSeqNo)
        if key not in self.commitsWaitingForPrepare:
            self.commitsWaitingForPrepare[key] = deque()
        self.commitsWaitingForPrepare[key].append((request, sender))

    def dequeueCommits(self, viewNo: int, ppSeqNo: int):
        key = (viewNo, ppSeqNo)
        if key in self.commitsWaitingForPrepare:
            i = 0
            # Keys of pending prepares that will be processed below
            while self.commitsWaitingForPrepare[key]:
                commit, sender = self.commitsWaitingForPrepare[
                    key].popleft()
                logger.debug("{} popping stashed COMMIT{}".format(self, key))
                self.processCommit(commit, sender)
                i += 1
            self.commitsWaitingForPrepare.pop(key)
            logger.debug("{} processed {} COMMITs waiting for PREPARE for"
                         " view no {} and seq no {}".
                         format(self, i, viewNo, ppSeqNo))

    def getDigestFor3PhaseKey(self, key: ThreePhaseKey) -> Optional[str]:
        reqKey = self.getReqKeyFrom3PhaseKey(key)
        digest = self.requests.digest(reqKey)
        if not digest:
            logger.debug("{} could not find digest in sent or received "
                         "PRE-PREPAREs or PREPAREs for 3 phase key {} and req "
                         "key {}".format(self, key, reqKey))
            return None
        else:
            return digest

    def getReqKeyFrom3PhaseKey(self, key: ThreePhaseKey):
        reqKey = None
        if key in self.sentPrePrepares:
            reqKey = self.sentPrePrepares[key][0]
        elif key in self.prePrepares:
            reqKey = self.prePrepares[key][0]
        elif key in self.prepares:
            reqKey = self.prepares[key][0]
        else:
            logger.debug("Could not find request key for 3 phase key {}".
                         format(key))
        return reqKey

    def can_pp_seq_no_be_in_view(self, view_no, pp_seq_no):
        """
        Checks if the `pp_seq_no` could have been in view `view_no`. It will
        return False when the `pp_seq_no` belongs to a later view than
        `view_no` else will return True
        :return:
        """
        assert view_no <= self.viewNo
        return view_no == self.viewNo or (view_no < self.viewNo and (
                        view_no in self.view_ends_at and
                        pp_seq_no <= self.view_ends_at[view_no][0]))

    @property
    def threePhaseState(self):
        # TODO: This method is incomplete
        # Gets the current stable and unstable checkpoints and creates digest
        # of unstable checkpoints
        if self.checkpoints:
            pass
        else:
            state = []
        return ThreePCState(self.instId, state)

    def process3PhaseState(self, msg: ThreePCState, sender: str):
        # TODO: This is not complete
        pass

    def send(self, msg, stat=None) -> None:
        """
        Send a message to the node on which this replica resides.

        :param stat:
        :param rid: remote id of one recipient (sends to all recipients if None)
        :param msg: the message to send
        """
        logger.display("{} sending {}".format(self, msg.__class__.__name__),
                       extra={"cli": True, "tags": ['sending']})
        logger.trace("{} sending {}".format(self, msg))
        if stat:
            self.stats.inc(stat)
        self.outBox.append(msg)
