import logging
from collections import defaultdict
from queue import PriorityQueue

from teether.util.frontierset import FrontierSet


class BackwardExplorerState(object):
    def __init__(self, bb, gas, must_visit, cost, data):
        self.bb = bb
        self.gas = gas
        self.must_visit = must_visit.copy()
        self.data = data
        self.cost = cost

    def estimate(self):
        """
        Return an estimate of how quickly we can reach the root of the tree
        This estimate is the sum of the number of branches taken so far (self.cost) and the
        estimate given by the next BB to visit (self.bb.estimate)
        返回我们到达树根的估计速度，这个估计是到目前为止所获得的分支数量(self.cost)和下一个要访问的BB给出的估计(self.bb.estimate)的总和。
        :return: estimated distance to root
        :返回:到根的估计距离
        """
        if self.bb.estimate_constraints is None:
            return self.cost
        else:
            return self.cost + self.bb.estimate_constraints

    def rank(self):
        """
        Compute a rank for this state. Order by estimated root-distance first, solve ties by favoring less restricted states
        for caching efficiency计算这个状态的等级。首先通过估计根距离排序，通过倾向于较少限制的状态来解决连接，以提高缓存效率
        :return:
        """
        return self.estimate(), len(self.must_visit)

    def __lt__(self, other):#<运算符重载
        return self.rank() < other.rank()

    def __hash__(self):
        return sum(a * b for a, b in zip((23, 29, 31), (hash(self.bb), hash(self.must_visit), hash(self.data))))

    def __eq__(self, other):
        return self.bb == other.bb and self.must_visit == other.must_visit and self.data == other.data

    def __str__(self):
        return 'At: %x, Gas: %s, Must-Visit: %s, Data: %s, Hash: %x' % (
        self.bb.start, self.gas, self.must_visit, self.data, hash(self))


def generate_sucessors(state, new_data, update_data, predicate=lambda st, pred: True):
    new_todo = []
    if state.gas is None or state.gas > 0:#无限gas或者有gas
        # logging.debug('[tr] [gs] passed first if')
        new_gas = state.gas
        if state.gas and len(state.bb.pred) > 1:
            new_gas = state.gas - 1
        # logging.debug('[tr] [gs] Preds: %s', state.bb.pred)

        for p in state.bb.pred:
            if not predicate(state.data, p):
                continue

            new_must_visits = []
            for path in state.bb.pred_paths[p]:
                new_must_visit = state.must_visit.copy()
                for a, b in zip(path[:-1], path[1:]):
                    new_must_visit.add(b, a)
                if p.start in new_must_visit.frontier:
                    new_must_visit.remove(p.start)
                if not new_must_visit.all.issubset(p.ancestors):
                    # logging.debug('[tr] [gs] Cannot reach any necessary states, aborting! Needed: %s, reachable: %s', new_must_visit, p.ancestors)
                    continue
                new_must_visits.append(new_must_visit)

            new_cost = state.cost + (1 if p.branch else 0)

            for new_must_visit in minimize(new_must_visits):
                new_todo.append(BackwardExplorerState(p, new_gas, new_must_visit, new_cost, update_data(new_data, p)))
    return new_todo


def traverse_back(start_ins, initial_gas, initial_data, advance_data, update_data, finish_path, must_visits=[],
                  predicate=lambda st, p: True):
    """


    :param start_ins: Starting instructions
    :param initial_gas: Starting "gas". Can be None, in which case it is unlimited
    :param initial_data: Starting data
    :param advance_data: method to advance data
    :param update_data: method to update data
    :param must_visits: FrontierSet describing the next nodes that *must* be visited
    :param predicate: A function (state, BB) -> Bool describing whether an edge should be taken or not
    :return: yields paths as they are explored one-by-one
    :param start_ins:启动指令
    :param initial_gas:启动gas。可以是None，在这种情况下它是无限的
    :param initial_data:启动数据
    :参数advance_data:高级数据的方法
    :参数update_data:更新数据的方法
    :param must_visits: FrontierSet描述下一个必须访问的节点
    :param predicate:一个函数(state, BB) -> Bool，用于描述是否应该取边
    :return:生成一个个路径
    """
    todo = PriorityQueue()

    for ins in start_ins:
        # logging.debug('[tr] Starting traversal at %x', ins.addr)
        data = initial_data(ins)
        bb = ins.bb
        gas = initial_gas
        # keep tuples of (len(must_visit), state)
        # this way, the least restricted state are preferred
        # which should maximize caching efficiency
        if not must_visits:
            must_visits = [FrontierSet()]
        for must_visit in minimize(FrontierSet(mv) if mv is not FrontierSet else mv for mv in must_visits):
            ts = BackwardExplorerState(bb, gas, must_visit, 0, data)
            todo.put(ts)

    cache = set()
    ended_prematurely = defaultdict(int)
    while not todo.empty():
        state = todo.get()
        # if this BB can be reached via multiple paths, check if we want to cache it
        # or whether another path already reached it with the same state
        # 如果这个BB可以通过多个路径到达，检查我们是否想要缓存它，或者其他路径是否已经以相同的状态到达它
        if len(state.bb.succ) > 1:
            if state in cache:
                # logging.debug('[tr] CACHE HIT')
                continue
            cache.add(state)
        # logging.debug('[tr] Cachesize: %d\t(slicing %x, currently at %x)', len(cache), ins.addr, state.bb.start)
        # logging.debug('[tr] Current state: %s', state)
        new_data = advance_data(state.data)
        if finish_path(new_data):
            # logging.debug('[tr] finished path (%s)', new_data)
            yield new_data
        else:
            if state.gas is not None and state.bb.estimate_back_branches is not None and (state.gas == 0 or state.gas < state.bb.estimate_back_branches):
                ended_prematurely[state.bb.start] += 1
            else:
                # logging.debug('[tr] continuing path (%s)', new_data)
                new_todo = generate_sucessors(state, new_data, update_data, predicate=predicate)
                for nt in new_todo:
                    todo.put(nt)
    total_ended = sum(ended_prematurely.values())
    if total_ended:
        logging.info("%d paths that ended prematurely due to branches: %s", total_ended,
                     ', '.join('%x: %d' % (k, v) for k, v in ended_prematurely.items()))
    else:
        logging.info("Finished all paths")


def minimize(must_visits):
    todo = sorted(must_visits, key=len)
    while todo:
        must_visit = todo[0]
        yield must_visit
        todo = [mv for mv in todo[1:] if not must_visit.issubset(mv)]
