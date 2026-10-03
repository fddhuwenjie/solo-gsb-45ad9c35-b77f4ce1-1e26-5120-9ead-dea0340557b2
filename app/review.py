"""审阅依赖编排的图算法（纯函数，不落库）。

依赖图以批注编号为节点、「前置批注 → 后续批注」为有向边：
后续批注（dependent）须等全部前置批注（prerequisite）完成后才可处理。
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set, Tuple

Edge = Tuple[int, int]  # (prerequisite_id, dependent_id)


def build_prereq_map(edges: Iterable[Edge]) -> Dict[int, Set[int]]:
    """由边集构建「后续批注 → 直接前置批注集合」的映射。"""
    prereqs: Dict[int, Set[int]] = {}
    for pre, dep in edges:
        prereqs.setdefault(dep, set()).add(pre)
    return prereqs


def transitive_prerequisites(prereqs: Dict[int, Set[int]], start: int) -> Set[int]:
    """``start`` 的全部传递前置项（沿前置边可达的全部祖先，不含自身）。"""
    seen: Set[int] = set()
    stack = list(prereqs.get(start, ()))
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(prereqs.get(node, ()))
    return seen


def _reverse_adjacency(adjacency: Dict[int, List[int]]) -> Dict[int, List[int]]:
    """邻接表取反：``rev[v]`` 为所有以 ``v`` 为终点的边的起点。"""
    rev: Dict[int, List[int]] = {n: [] for n in adjacency}
    for pre, deps in adjacency.items():
        for dep in deps:
            rev[dep].append(pre)
    return rev


def _cyclic_component_minima(
    sorted_nodes: List[int],
    adjacency: Dict[int, List[int]],
    rev: Dict[int, List[int]],
) -> List[int]:
    """Kosaraju 求强连通分量，返回各「含环」分量中的最小节点。

    含环 = 分量内节点数大于 1，或唯一节点带自环。两次 DFS 均为
    O(V+E)；分量划分与遍历顺序无关。
    """
    # 第一遍：原图后序
    visited: Set[int] = set()
    order: List[int] = []
    for root in sorted_nodes:
        if root in visited:
            continue
        visited.add(root)
        dfs_stack: List[Tuple[int, int]] = [(root, 0)]
        while dfs_stack:
            node, idx = dfs_stack[-1]
            neighbors = adjacency[node]
            if idx < len(neighbors):
                dfs_stack[-1] = (node, idx + 1)
                nxt = neighbors[idx]
                if nxt not in visited:
                    visited.add(nxt)
                    dfs_stack.append((nxt, 0))
            else:
                order.append(node)
                dfs_stack.pop()

    # 第二遍：按逆后序在反图上收缩分量
    assigned: Set[int] = set()
    minima: List[int] = []
    for root in reversed(order):
        if root in assigned:
            continue
        component: List[int] = []
        stack = [root]
        assigned.add(root)
        while stack:
            node = stack.pop()
            component.append(node)
            for nxt in rev[node]:
                if nxt not in assigned:
                    assigned.add(nxt)
                    stack.append(nxt)
        if len(component) > 1:
            minima.append(min(component))
        elif root in adjacency[root]:  # 单节点分量：自环才算含环
            minima.append(root)
    return minima


def find_cycle_path(edges: Iterable[Edge]) -> Optional[List[int]]:
    """检测环并返回闭环路径；无环时返回 ``None``。

    按批注编号升序确定起点、按邻接编号升序分支，返回首条以环中
    最小编号开头的闭环路径，如 ``[2, 3, 5, 2]``。遍历顺序固定，
    相同输入必然得到相同路径。

    实现分两步，全程 O(V+E)（不弱于 O(V(V+E))），不枚举简单路径：
    1. Kosaraju 强连通分量定位「按编号升序第一个落在含环 SCC 中
       的节点」``start`` —— 它正是旧实现逐起点枚举时首个闭环环上
       的最小编号；
    2. 只在编号 ≥ start 的诱导子图上，从 start 做一次邻接升序 DFS，
       首条回到灰色 start 的边沿父指针还原为闭环路径。这与旧实现
       从该起点枚举简单路径时首次闭合的路径完全一致（小于 start
       的节点不可能参与任何回到 start 的环，否则 start 不是最小环点）。
    """
    adj_sets: Dict[int, Set[int]] = {}
    nodes: Set[int] = set()
    for pre, dep in edges:
        adj_sets.setdefault(pre, set()).add(dep)
        nodes.add(pre)
        nodes.add(dep)
    if not nodes:
        return None
    adjacency = {n: sorted(adj_sets.get(n, ())) for n in nodes}
    rev = _reverse_adjacency(adjacency)

    sorted_nodes = sorted(nodes)
    minima = _cyclic_component_minima(sorted_nodes, adjacency, rev)
    if not minima:
        return None
    start = min(minima)

    # 在「编号 ≥ start」子图上做一次迭代式 DFS
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[int, int] = {start: GRAY}
    parent: Dict[int, int] = {}
    stack: List[Tuple[int, int]] = [(start, 0)]
    while stack:
        node, idx = stack[-1]
        neighbors = adjacency[node]
        if idx >= len(neighbors):
            color[node] = BLACK
            stack.pop()
            continue
        stack[-1] = (node, idx + 1)
        nxt = neighbors[idx]
        if nxt < start:
            continue
        if nxt == start:
            # 回到搜索栈上的起点（start 在本次 DFS 中永不变黑）：
            # 沿父指针还原 DFS 树路径并闭环
            path = [node]
            while path[-1] != start:
                path.append(parent[path[-1]])
            path.reverse()
            return path + [start]
        if color.get(nxt, WHITE) == WHITE:
            color[nxt] = GRAY
            parent[nxt] = node
            stack.append((nxt, 0))
        # 指向其他 GRAY 祖先的边属于「最小节点更大」的环，跳过；
        # 指向 BLACK 节点的为跨边/前向边，同样跳过。
    return None  # 理论不可达：start 位于含环 SCC，必然能回到自身


def topological_waves(
    node_ids: Iterable[int],
    prereqs: Dict[int, Set[int]],
    completed: Set[int],
) -> List[List[int]]:
    """把未完成节点按拓扑层级分批：同层节点互不依赖，可并行处理。

    第 0 层为当前即可处理（全部前置均已完成）的节点；第 k 层为
    前置全部落在已完成集合或前 k-1 层中的节点。图无环时每个未
    完成节点恰好落入一层（依赖写入前已检测环，此处防御性报错）。
    """
    remaining = set(node_ids) - set(completed)
    satisfied = set(completed)
    waves: List[List[int]] = []
    while remaining:
        wave = sorted(
            n for n in remaining if prereqs.get(n, frozenset()) <= satisfied
        )
        if not wave:
            raise ValueError("依赖图存在环，无法拓扑分层")
        waves.append(wave)
        satisfied.update(wave)
        remaining.difference_update(wave)
    return waves
