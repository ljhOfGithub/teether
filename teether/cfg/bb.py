import logging
from collections import defaultdict, deque

from teether.util.utils import unique


class BB(object):
    def __init__(self, ins):
        self.ins = ins
        self.streads = set()  # indices of stack-items that will be read by this BB (0 is the topmost item on stack)
        # 将被这个BB读取的堆栈项的索引(0是堆栈上最上面的项)
        self.stwrites = set()  # indices of stack-items that will be written by this BB (0 is the topmost item on stack)
        # 将由这个BB写入的堆栈项的索引(0是堆栈上最上面的项)
        self.stdelta = 0
        for i in ins:
            i.bb = self
            if 0x80 <= i.op <= 0x8f:  # Special handling for DUP
                ridx = i.op - 0x80 - self.stdelta#根据evm文档获得实际的dup指令
                widx = -1 - self.stdelta#计算实际将要写入的位置，栈顶
                if ridx not in self.stwrites:
                    self.streads.add(ridx)#将要读取的位置，倒数第ridx个
                self.stwrites.add(widx)
            elif 0x90 <= i.op <= 0x9f:  # Special handling for SWAP
                idx1 = i.op - 0x8f - self.stdelta
                idx2 = - self.stdelta
                if idx1 not in self.stwrites:#如果该位置不会被写入，则读取该位置的值为后面的复制做准备
                    self.streads.add(idx1)
                if idx2 not in self.stwrites:
                    self.streads.add(idx2)
                self.stwrites.add(idx1)
                self.stwrites.add(idx2)
            else:  # assume entire stack is affected otherwise假设整个堆栈受到影响
                for j in range(i.ins):
                    idx = j - self.stdelta
                    if idx not in self.stwrites:
                        self.streads.add(idx)
                for j in range(i.outs):
                    idx = i.ins - 1 - j - self.stdelta
                    self.stwrites.add(idx)
            self.stdelta += i.delta
        self.streads = {x for x in self.streads if x >= 0}#找原集合中的非负的位置
        self.stwrites = {x for x in self.stwrites if x >= 0}
        self.start = self.ins[0].addr
        self.pred = set()
        self.succ = set()
        self.succ_addrs = set()
        self.pred_paths = defaultdict(set)
        self.branch = self.ins[-1].op == 0x57
        self.indirect_jump = self.ins[-1].op in (0x56, 0x57)
        self.ancestors = set()
        self.descendants = set()
        # maintain a set of 'must_visit' constraints to limit
        # backward-slices to only new slices after new edges are added
        # initially, no constraint is given (= empty set)
        # 维护一组'must_visit'约束，以限制向后切片仅在初始添加新边后的新切片，没有给出约束(=空集)
        self.must_visit = [set()]
        # also maintain an estimate of how fast we can get from here
        # to the root of the cfg
        # how fast meaning, how many JUMPI-branches we have to take
        #还可以估算从这里到cfg根的速度有多快，也就是我们需要多少个跳转分支
        self.estimate_constraints = (1 if self.branch else 0) if self.start == 0 else None
        # start=0且branch=1时estimate_constraints为1，start=0且branch=0时estimate_constraints为0，start=1则为none，后面的优先判断
        # and another estimate fo many backwards branches
        # we will encounter to the root另一个估计是我们会遇到很多向后的分支
        self.estimate_back_branches = 0 if self.start == 0 else None 

    @property
    def jump_resolved(self):
        return not self.indirect_jump or len(self.must_visit) == 0

    def update_ancestors(self, new_ancestors):
        new_ancestors = new_ancestors - self.ancestors
        if new_ancestors:
            self.ancestors.update(new_ancestors)
            for s in self.succ:
                s.update_ancestors(new_ancestors)

    def update_descendants(self, new_descendants):
        new_descendants = new_descendants - self.descendants
        if new_descendants:
            self.descendants.update(new_descendants)
            for p in self.pred:
                p.update_descendants(new_descendants)

    def update_estimate_constraints(self):
        if all(p.estimate_constraints is None for p in self.pred):#all() 函数用于判断给定的可迭代参数 iterable 中的所有元素是否都为 TRUE，如果是返回 True，否则返回 False。
            return#p是self.pred集合中的一个对象，有属性estimate_constraints，如果每个BB对象的estimate_constraints都是None则直接返回
        best_estimate = min(p.estimate_constraints for p in self.pred if p.estimate_constraints is not None)
        if self.branch:
            best_estimate += 1
        if self.estimate_constraints is None or best_estimate < self.estimate_constraints:
            self.estimate_constraints = best_estimate
            for s in self.succ:
                s.update_estimate_constraints()

    def update_estimate_back_branches(self):
        if all(p.estimate_back_branches is None for p in self.pred):
            return
        best_estimate = min(p.estimate_back_branches for p in self.pred if p.estimate_back_branches is not None)
        if len(self.pred) > 1:
            best_estimate += 1
        if self.estimate_back_branches is None or best_estimate != self.estimate_back_branches:
            self.estimate_back_branches = best_estimate
            for s in self.succ:
                s.update_estimate_back_branches()

    def add_succ(self, other, path):
        self.succ.add(other)
        other.pred.add(self)
        self.update_descendants(other.descendants | {other.start})
        other.update_ancestors(self.ancestors | {self.start})
        other.update_estimate_constraints()
        other.update_estimate_back_branches()
        other.pred_paths[self].add(tuple(path))
        seen = set()
        todo = deque()
        todo.append(other)
        while todo:
            bb = todo.popleft()
            if bb not in seen:
                seen.add(bb)
                if bb.indirect_jump:
                    bb.must_visit.append({self.start})
                # logging.debug('BB@%x, must_visit: %s', bb.start, bb.must_visit)
                todo.extend(s for s in bb.succ if s not in seen)

    def _find_jump_target(self):
        if len(self.ins) >= 2 and 0x60 <= self.ins[-2].op <= 0x71:
            self.must_visit = []
            return int.from_bytes(self.ins[-2].arg, byteorder='big')
        else:
            return None

    def get_succ_addrs_full(self, valid_jump_targets):
        from teether.slicing import slice_to_program, backward_slice
        from teether.evm.exceptions import ExternalData
        from teether.memory import UninitializedRead
        from teether.evm.evm import run
        new_succ_addrs = set()
        if self.indirect_jump and not self.jump_resolved:
            bs = backward_slice(self.ins[-1], [0], must_visits=self.must_visit)
            for b in bs:
                if 0x60 <= b[-1].op <= 0x7f:
                    succ_addr = int.from_bytes(b[-1].arg, byteorder='big')
                else:
                    p = slice_to_program(b)
                    try:
                        succ_addr = run(p, check_initialized=True).stack.pop()
                    except (ExternalData, UninitializedRead):
                        logging.warning('Failed to compute jump target for BB@{}, slice: \n{}'.format(self.start, '\n'.join('\t{}'.format(ins) for ins in b)))
                        continue
                if succ_addr not in valid_jump_targets:
                    logging.warning('Jump to invalid address')
                    continue
                path = tuple(unique(ins.bb.start for ins in b if ins.bb))
                if succ_addr not in self.succ_addrs:
                    self.succ_addrs.add(succ_addr)
                if (path, succ_addr) not in new_succ_addrs:
                    new_succ_addrs.add((path, succ_addr))
        # We did our best,
        # if someone finds a new edge, jump_resolved will be set to False by the BFS in add_succ
        # 我们已经尽了最大努力，如果有人找到了一个新的边，则在add_succ中BFS将jump_resolved设置为False
        self.must_visit = []
        return self.succ_addrs, new_succ_addrs

    def get_succ_addrs(self, valid_jump_targets):
        if self.ins[-1].op in (0x56, 0x57):
            jump_target = self._find_jump_target()
            if jump_target is not None:
                self.indirect_jump = False
                if jump_target in valid_jump_targets:
                    self.succ_addrs.add(jump_target)
            else:
                self.indirect_jump = True
        else:
            self.must_visit = []
        if self.ins[-1].op not in (0x00, 0x56, 0xf3, 0xfd, 0xfe, 0xff):
            fallthrough = self.ins[-1].next_addr
            if fallthrough:
                self.succ_addrs.add(fallthrough)
        return self.succ_addrs

    def __str__(self):
        s = 'BB @ %x\tStack %d' % (self.start, self.stdelta)
        s += '\n'
        s += 'Stackreads: {%s}' % (', '.join(map(str, sorted(self.streads))))
        s += '\n'
        s += 'Stackwrites: {%s}' % (', '.join(map(str, sorted(self.stwrites))))
        if self.pred:
            s += '\n'
            s += '\n'.join('%x ->' % pred.start for pred in self.pred)
        s += '\n'
        s += '\n'.join(str(ins) for ins in self.ins)
        if self.succ:
            s += '\n'
            s += '\n'.join(' -> %x' % succ.start for succ in self.succ)
        return s

    def __repr__(self):
        return str(self)

    def __lt__(self, other):
        return self.start < other.start
