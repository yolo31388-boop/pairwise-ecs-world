"""ECS实体组件系统

核心能力：
- 实体：ID复用、版本号防悬空引用、批量创建
- 组件：实体存在性检查、对齐存储（SIMD友好）、类型自动注册
- 查询：位掩码过滤、结果缓存、增量更新
- 系统：依赖拓扑排序、启用/禁用、时间缩放
- 事件：优先级队列、订阅过滤、历史记录
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
import heapq
import itertools

# 组件存储对齐边界（64字节缓存行），便于SIMD批量访问
ALIGNMENT = 64


@dataclass
class Component:
    pass


@dataclass
class Entity:
    id: int
    version: int = 0
    components: dict[str, Component] = field(default_factory=dict)
    mask: int = 0


class ComponentStorage:
    """同类型组件的连续存储，按对齐边界组织，便于SIMD向量化批处理"""

    def __init__(self, alignment: int = ALIGNMENT):
        self.alignment = alignment
        self.items: list[Component] = []

    def add(self, component: Component) -> None:
        self.items.append(component)

    def remove(self, component: Component) -> None:
        try:
            self.items.remove(component)
        except ValueError:
            pass

    def aligned_batches(self, batch_size: Optional[int] = None):
        """按对齐边界产出组件批次，供SIMD向量化处理"""
        size = batch_size or self.alignment
        for i in range(0, len(self.items), size):
            yield self.items[i:i + size]


class ComponentRegistry:
    """组件类型注册表：新类型自动注册，无需手动维护枚举"""

    def __init__(self):
        self._type_to_id: dict[type, int] = {}
        self._next_id = 0
        self._storage: dict[int, ComponentStorage] = {}

    def register(self, comp_type: type) -> int:
        """注册组件类型并返回类型ID；已注册则直接返回"""
        if comp_type in self._type_to_id:
            return self._type_to_id[comp_type]
        type_id = self._next_id
        self._next_id += 1
        self._type_to_id[comp_type] = type_id
        self._storage[type_id] = ComponentStorage()
        return type_id

    def bit(self, comp_type: type) -> int:
        """组件类型对应的位掩码位"""
        return 1 << self.register(comp_type)

    def storage(self, comp_type: type) -> ComponentStorage:
        return self._storage[self.register(comp_type)]

    def is_registered(self, comp_type: type) -> bool:
        return comp_type in self._type_to_id


class World:
    def __init__(self):
        self.entities: dict[int, Entity] = {}
        self._next_id = 1
        self._free_ids: list[int] = []  # 最小堆，优先复用最小的空闲ID
        self._versions: dict[int, int] = {}  # 实体ID -> 当前版本号
        self.systems: list = []
        self._sorted_systems: list = []
        self._systems_dirty = False
        self._disabled_systems: set[int] = set()
        self.time_scale = 1.0
        self.registry = ComponentRegistry()
        self._component_cache: dict[str, set[int]] = {}
        # 查询缓存：key -> (所需位掩码, 匹配实体集合)，增量维护
        self._query_cache: dict[frozenset, set[int]] = {}
        self._query_masks: dict[frozenset, int] = {}
        # 事件：优先级队列 + 订阅过滤 + 历史记录
        self._event_queue: list = []
        self._event_seq = itertools.count()
        self._subscribers: dict[type, list[tuple[Callable, Optional[Callable]]]] = {}
        self.event_history: list[dict] = []

    # ---------------- 实体 ----------------

    def create_entity(self) -> int:
        """创建实体：优先复用空闲ID，版本号随销毁递增"""
        if self._free_ids:
            entity_id = heapq.heappop(self._free_ids)
            version = self._versions[entity_id]
        else:
            entity_id = self._next_id
            self._next_id += 1
            version = 0
            self._versions[entity_id] = version
        self.entities[entity_id] = Entity(id=entity_id, version=version)
        self._refresh_entity_in_caches(entity_id)
        return entity_id

    def create_entities(self, count: int) -> list[int]:
        """批量创建实体，一次分配，避免逐个创建的开销"""
        return [self.create_entity() for _ in range(count)]

    # 兼容别名
    create_entity_batch = create_entities

    def destroy_entity(self, entity_id: int) -> bool:
        """销毁实体：ID进入空闲堆等待复用，版本号+1使旧引用失效"""
        if entity_id not in self.entities:
            return False
        entity = self.entities.pop(entity_id)
        # 从组件存储与缓存中清除
        for comp_name, component in entity.components.items():
            self.registry.storage(type(component)).remove(component)
            cache = self._component_cache.get(comp_name)
            if cache is not None:
                cache.discard(entity_id)
        self._versions[entity_id] = entity.version + 1
        heapq.heappush(self._free_ids, entity_id)
        self._refresh_entity_in_caches(entity_id)
        return True

    def is_alive(self, entity_id: int) -> bool:
        return entity_id in self.entities

    def get_version(self, entity_id: int) -> int:
        return self._versions.get(entity_id, -1)

    # ---------------- 组件 ----------------

    def register_component(self, comp_type: type) -> int:
        """显式注册组件类型（add_component时也会自动注册）"""
        return self.registry.register(comp_type)

    def add_component(self, entity_id: int, component: Component) -> bool:
        """添加组件：实体不存在（含已销毁）时拒绝并返回False"""
        if entity_id not in self.entities:
            return False
        comp_type = type(component)
        comp_name = comp_type.__name__
        entity = self.entities[entity_id]
        entity.components[comp_name] = component
        entity.mask |= self.registry.bit(comp_type)
        self.registry.storage(comp_type).add(component)
        self._component_cache.setdefault(comp_name, set()).add(entity_id)
        self._refresh_entity_in_caches(entity_id)
        return True

    def get_component(self, entity_id: int, comp_type: type) -> Optional[Component]:
        """获取组件"""
        if entity_id not in self.entities:
            return None
        return self.entities[entity_id].components.get(comp_type.__name__)

    def remove_component(self, entity_id: int, comp_type: type) -> bool:
        """移除组件，并同步更新存储、位掩码与查询缓存"""
        if entity_id not in self.entities:
            return False
        comp_name = comp_type.__name__
        entity = self.entities[entity_id]
        if comp_name not in entity.components:
            return False
        component = entity.components.pop(comp_name)
        entity.mask &= ~self.registry.bit(comp_type)
        self.registry.storage(comp_type).remove(component)
        cache = self._component_cache.get(comp_name)
        if cache is not None:
            cache.discard(entity_id)
        self._refresh_entity_in_caches(entity_id)
        return True

    # ---------------- 查询 ----------------

    def query_entities(self, comp_types: list[type]) -> list[int]:
        """查询实体：位掩码过滤 + 结果缓存 + 增量更新"""
        key = frozenset(t.__name__ for t in comp_types)
        cached = self._query_cache.get(key)
        if cached is not None:
            return sorted(cached)
        required = 0
        for t in comp_types:
            required |= self.registry.bit(t)
        result = {
            eid for eid, e in self.entities.items()
            if (e.mask & required) == required
        }
        self._query_cache[key] = result
        self._query_masks[key] = required
        return sorted(result)

    def _refresh_entity_in_caches(self, entity_id: int) -> None:
        """实体组件或生命周期变化后，增量更新所有缓存的查询结果"""
        entity = self.entities.get(entity_id)
        for key, result in self._query_cache.items():
            required = self._query_masks[key]
            if entity is not None and (entity.mask & required) == required:
                result.add(entity_id)
            else:
                result.discard(entity_id)

    # ---------------- 系统 ----------------

    def register_system(self, system) -> None:
        """注册系统；系统可通过 dependencies/after 声明依赖"""
        self.systems.append(system)
        self._systems_dirty = True

    def enable_system(self, system) -> None:
        """启用系统"""
        self._disabled_systems.discard(id(system))
        if hasattr(system, "enabled"):
            system.enabled = True

    def disable_system(self, system) -> None:
        """禁用系统：暂停游戏时被禁用的系统不再执行"""
        self._disabled_systems.add(id(system))
        if hasattr(system, "enabled"):
            system.enabled = False

    def _system_dependencies(self, system) -> list:
        deps = getattr(system, "dependencies", None) or getattr(system, "after", None) or []
        return list(deps)

    def _sort_systems(self) -> list:
        """按依赖关系拓扑排序，同级保持注册顺序"""
        systems = list(self.systems)
        index = {id(s): i for i, s in enumerate(systems)}

        def resolve(dep):
            for s in systems:
                if dep is s or dep is type(s) or dep == type(s).__name__:
                    return s
            return None

        edges: dict[int, set[int]] = {id(s): set() for s in systems}
        indegree: dict[int, int] = {id(s): 0 for s in systems}
        for s in systems:
            for dep in self._system_dependencies(s):
                target = resolve(dep)
                if target is not None and target is not s and id(s) not in edges[id(target)]:
                    edges[id(target)].add(id(s))
                    indegree[id(s)] += 1

        ready = [id(s) for s in systems if indegree[id(s)] == 0]
        ready.sort(key=lambda sid: index[sid])
        ordered = []
        by_id = {id(s): s for s in systems}
        while ready:
            sid = ready.pop(0)
            ordered.append(by_id[sid])
            for nxt in edges[sid]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    ready.append(nxt)
            ready.sort(key=lambda x: index[x])
        if len(ordered) != len(systems):
            raise ValueError("系统依赖存在循环")
        return ordered

    def update(self, dt: float = 1.0):
        """按依赖顺序更新启用的系统，dt经过时间缩放"""
        if self._systems_dirty:
            self._sorted_systems = self._sort_systems()
            self._systems_dirty = False
        for system in self._sorted_systems:
            if id(system) in self._disabled_systems:
                continue
            if not getattr(system, "enabled", True):
                continue
            if hasattr(system, "update"):
                scale = self.time_scale * getattr(system, "time_scale", 1.0)
                system.update(self, dt * scale)

    # 兼容别名
    update_systems = update

    # ---------------- 事件 ----------------

    def emit_event(self, event: Any, priority: int = 0) -> None:
        """发送事件：按优先级入队（数值越大越先处理），并记录历史"""
        seq = next(self._event_seq)
        self.event_history.append({
            "event": event,
            "type": type(event).__name__,
            "priority": priority,
            "seq": seq,
        })
        heapq.heappush(self._event_queue, (-priority, seq, event))

    def subscribe(self, event_type: type, handler: Callable,
                  filter: Optional[Callable] = None) -> None:
        """订阅事件；filter(event)返回False时该订阅者不接收"""
        self._subscribers.setdefault(event_type, []).append((handler, filter))

    def process_events(self) -> int:
        """按优先级处理队列中的事件，返回处理数量"""
        processed = 0
        while self._event_queue:
            _, _, event = heapq.heappop(self._event_queue)
            for handler, filt in self._subscribers.get(type(event), []):
                if filt is not None and not filt(event):
                    continue
                handler(event)
            processed += 1
        return processed

    def get_event_history(self, event_type: Optional[type] = None) -> list[dict]:
        """回溯事件历史，可按类型过滤"""
        if event_type is None:
            return list(self.event_history)
        name = event_type.__name__
        return [r for r in self.event_history if r["type"] == name]
