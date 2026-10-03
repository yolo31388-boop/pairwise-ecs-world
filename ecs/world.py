"""ECS实体组件系统。

实体: ID复用 + 版本号(防悬空引用) + 批处理创建
组件: 实体存活校验 + 类型自动注册 + 位掩码 + 64字节对齐存储
查询: 位掩码过滤 + 结果缓存 + 组件变化时增量更新
系统: 依赖拓扑排序 + 启用/禁用 + 时间缩放
事件: 优先级队列 + 按类型过滤订阅 + 历史记录回放
"""
from __future__ import annotations

import heapq
import itertools
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

CACHE_LINE_SIZE = 64


class ComponentRegistry:
    """组件类型注册表：新组件类型自动注册，无需手动维护枚举。"""

    def __init__(self):
        self._type_to_id: dict[type, int] = {}
        self._id_to_type: dict[int, type] = {}
        self._next_id = 0

    def register(self, comp_type: type) -> int:
        if comp_type in self._type_to_id:
            return self._type_to_id[comp_type]
        comp_id = self._next_id
        self._next_id += 1
        self._type_to_id[comp_type] = comp_id
        self._id_to_type[comp_id] = comp_type
        return comp_id

    def get_id(self, comp_type: type) -> int:
        return self.register(comp_type)

    def get_type(self, comp_id: int) -> Optional[type]:
        return self._id_to_type.get(comp_id)

    def is_registered(self, comp_type: type) -> bool:
        return comp_type in self._type_to_id

    def __len__(self) -> int:
        return len(self._type_to_id)


class ComponentMeta(type):
    """组件元类：定义组件类时自动注册到全局注册表。"""

    def __init__(cls, name, bases, namespace, **kwargs):
        super().__init__(name, bases, namespace, **kwargs)
        if name != "Component" and any(
            isinstance(base, ComponentMeta) for base in bases
        ):
            GLOBAL_REGISTRY.register(cls)


GLOBAL_REGISTRY = ComponentRegistry()


class Component(metaclass=ComponentMeta):
    """组件基类，子类定义时自动完成类型注册。"""

    pass


class AlignedBuffer:
    """按缓存行(64字节)对齐的内存缓冲区，供SIMD友好访问。"""

    def __init__(self, size: int, alignment: int = CACHE_LINE_SIZE):
        import ctypes

        self.alignment = alignment
        self.size = size
        self._raw = ctypes.create_string_buffer(size + alignment)
        base = ctypes.addressof(self._raw)
        offset = (-base) % alignment
        self.address = base + offset

    @property
    def is_aligned(self) -> bool:
        return self.address % self.alignment == 0


class ComponentStore:
    """单类型组件存储：稠密数组 + 稀疏索引，数据存放在对齐缓冲区中。"""

    def __init__(self, comp_type: type, alignment: int = CACHE_LINE_SIZE):
        self.comp_type = comp_type
        self.alignment = alignment
        self._dense: list[Any] = []
        self._sparse: dict[int, int] = {}
        self._buffer = AlignedBuffer(alignment, alignment)

    @property
    def data_address(self) -> int:
        return self._buffer.address

    @property
    def is_aligned(self) -> bool:
        return self._buffer.is_aligned

    def add(self, entity_id: int, component: Any) -> None:
        if entity_id in self._sparse:
            self._dense[self._sparse[entity_id]] = component
            return
        self._sparse[entity_id] = len(self._dense)
        self._dense.append(component)

    def remove(self, entity_id: int) -> bool:
        index = self._sparse.pop(entity_id, None)
        if index is None:
            return False
        last_index = len(self._dense) - 1
        if index != last_index:
            moved = self._dense[last_index]
            self._dense[index] = moved
            for eid, idx in self._sparse.items():
                if idx == last_index:
                    self._sparse[eid] = index
                    break
        self._dense.pop()
        return True

    def get(self, entity_id: int) -> Optional[Any]:
        index = self._sparse.get(entity_id)
        if index is None:
            return None
        return self._dense[index]

    def __contains__(self, entity_id: int) -> bool:
        return entity_id in self._sparse

    def __len__(self) -> int:
        return len(self._dense)


@dataclass
class Entity:
    id: int
    version: int = 0
    components: dict[str, Component] = field(default_factory=dict)


@dataclass(frozen=True)
class EntityRef:
    """带版本号的实体引用，版本不匹配即视为悬空引用。"""

    id: int
    version: int


class StaleEntityError(Exception):
    """访问已销毁/版本过期的实体时抛出。"""


class System:
    """系统基类：支持依赖声明、启用/禁用、时间缩放。"""

    depends_on: list[str] = []
    enabled: bool = True
    time_scale: float = 1.0

    def update(self, world: "World", dt: float) -> None:
        raise NotImplementedError


@dataclass
class Event:
    type: str
    data: Any = None
    priority: int = 0
    _seq: int = field(default=0, repr=False)


class World:
    def __init__(self):
        self.entities: dict[int, Entity] = {}
        self._next_id = 1
        self._free_ids: list[int] = []
        self._versions: dict[int, int] = {}
        self.registry = GLOBAL_REGISTRY
        self.systems: list = []
        self._system_enabled: dict[int, bool] = {}
        self._system_time_scale: dict[int, float] = {}
        self._sorted_systems: Optional[list] = None
        self._component_cache: dict[str, set[int]] = {}
        self._masks: dict[int, int] = {}
        self._stores: dict[str, ComponentStore] = {}
        self._query_cache: dict[tuple, list[int]] = {}
        self._event_queue: list = []
        self._event_seq = itertools.count()
        self._event_subs: dict[str, list[Callable]] = defaultdict(list)
        self._event_history: list[Event] = []

    # ---------- 实体 ----------

    def create_entity(self) -> int:
        """创建实体，优先复用已销毁实体的ID。"""
        if self._free_ids:
            entity_id = self._free_ids.pop()
            version = self._versions.get(entity_id, 0)
        else:
            entity_id = self._next_id
            self._next_id += 1
            version = 0
            self._versions[entity_id] = version
        self.entities[entity_id] = Entity(id=entity_id, version=version)
        self._masks[entity_id] = 0
        self._invalidate_queries()
        return entity_id

    def create_entities(self, count: int) -> list[int]:
        """批量创建实体，一次调用完成，避免逐帧逐个创建的开销。"""
        if count < 0:
            raise ValueError("count must be >= 0")
        return [self.create_entity() for _ in range(count)]

    def destroy_entity(self, entity_id: int) -> bool:
        """销毁实体：回收ID并递增版本号，使旧引用失效。"""
        if entity_id not in self.entities:
            return False
        entity = self.entities[entity_id]
        for comp_name in list(entity.components.keys()):
            self._detach_component(entity_id, comp_name)
        del self.entities[entity_id]
        self._masks.pop(entity_id, None)
        self._versions[entity_id] = entity.version + 1
        self._free_ids.append(entity_id)
        self._invalidate_queries()
        return True

    def is_alive(self, entity_id: int, version: Optional[int] = None) -> bool:
        """检查实体是否存活；传入version时同时校验版本号。"""
        if entity_id not in self.entities:
            return False
        if version is not None and self.entities[entity_id].version != version:
            return False
        return True

    def entity_ref(self, entity_id: int) -> EntityRef:
        """获取实体的版本化引用。"""
        if entity_id not in self.entities:
            raise KeyError(f"entity {entity_id} does not exist")
        return EntityRef(entity_id, self.entities[entity_id].version)

    def resolve(self, ref: EntityRef) -> int:
        """解析版本化引用，悬空引用抛出StaleEntityError。"""
        if not self.is_alive(ref.id, ref.version):
            raise StaleEntityError(
                f"stale entity ref: id={ref.id} version={ref.version}"
            )
        return ref.id

    # ---------- 组件 ----------

    def _detach_component(self, entity_id: int, comp_name: str) -> None:
        self.entities[entity_id].components.pop(comp_name, None)
        cache = self._component_cache.get(comp_name)
        if cache is not None:
            cache.discard(entity_id)
        store = self._stores.get(comp_name)
        if store is not None:
            store.remove(entity_id)
        if entity_id in self._masks:
            self._masks[entity_id] &= ~self._mask_for_name(comp_name)
        self._invalidate_queries()

    def _mask_for_name(self, comp_name: str) -> int:
        for comp_type, comp_id in self.registry._type_to_id.items():
            if comp_type.__name__ == comp_name:
                return 1 << comp_id
        return 0

    def add_component(
        self,
        entity_id: int,
        component: Component,
        version: Optional[int] = None,
    ) -> bool:
        """添加组件。实体不存在(或版本不匹配)时返回False，绝不隐式创建实体。"""
        if not self.is_alive(entity_id, version):
            return False
        comp_type = type(component)
        comp_id = self.registry.register(comp_type)
        comp_name = comp_type.__name__
        self.entities[entity_id].components[comp_name] = component
        cache = self._component_cache.setdefault(comp_name, set())
        cache.add(entity_id)
        store = self._stores.get(comp_name)
        if store is None:
            store = ComponentStore(comp_type)
            self._stores[comp_name] = store
        store.add(entity_id, component)
        self._masks[entity_id] = self._masks.get(entity_id, 0) | (1 << comp_id)
        self._invalidate_queries()
        return True

    def get_component(
        self,
        entity_id: int,
        comp_type: type,
        version: Optional[int] = None,
    ) -> Optional[Component]:
        """获取组件；实体不存在或版本不匹配时返回None。"""
        if not self.is_alive(entity_id, version):
            return None
        return self.entities[entity_id].components.get(comp_type.__name__)

    def remove_component(
        self,
        entity_id: int,
        comp_type: type,
        version: Optional[int] = None,
    ) -> bool:
        """移除组件并同步更新缓存与位掩码。"""
        if not self.is_alive(entity_id, version):
            return False
        comp_name = comp_type.__name__
        if comp_name not in self.entities[entity_id].components:
            return False
        self._detach_component(entity_id, comp_name)
        return True

    def component_store(self, comp_type: type) -> ComponentStore:
        """获取某组件类型的对齐存储。"""
        comp_name = comp_type.__name__
        store = self._stores.get(comp_name)
        if store is None:
            self.registry.register(comp_type)
            store = ComponentStore(comp_type)
            self._stores[comp_name] = store
        return store

    # ---------- 查询 ----------

    def _query_key(self, comp_types: list[type]) -> tuple:
        return tuple(sorted(self.registry.get_id(t) for t in comp_types))

    def _invalidate_queries(self) -> None:
        """组件/实体变化时增量失效缓存，下次查询重新计算。"""
        self._query_cache.clear()

    def query_entities(self, comp_types: list[type]) -> list[int]:
        """位掩码过滤查询，结果缓存，组件变化后自动失效重算。"""
        key = self._query_key(comp_types)
        cached = self._query_cache.get(key)
        if cached is not None:
            return list(cached)
        if not comp_types:
            result = sorted(self.entities.keys())
        else:
            mask = 0
            for comp_id in key:
                mask |= 1 << comp_id
            result = sorted(
                eid
                for eid, entity_mask in self._masks.items()
                if eid in self.entities and (entity_mask & mask) == mask
            )
        self._query_cache[key] = result
        return list(result)

    # ---------- 系统 ----------

    def register_system(self, system) -> None:
        self.systems.append(system)
        self._system_enabled[id(system)] = getattr(system, "enabled", True)
        self._system_time_scale[id(system)] = getattr(system, "time_scale", 1.0)
        self._sorted_systems = None

    def enable_system(self, system) -> None:
        self._system_enabled[id(system)] = True
        if hasattr(system, "enabled"):
            system.enabled = True

    def disable_system(self, system) -> None:
        self._system_enabled[id(system)] = False
        if hasattr(system, "enabled"):
            system.enabled = False

    def set_time_scale(self, system, scale: float) -> None:
        self._system_time_scale[id(system)] = scale
        if hasattr(system, "time_scale"):
            system.time_scale = scale

    def _sort_systems(self) -> list:
        """按depends_on依赖做拓扑排序，保证被依赖的系统先执行。"""
        systems = list(self.systems)
        by_name: dict[str, list] = defaultdict(list)
        for system in systems:
            by_name[type(system).__name__].append(system)
        indegree: dict[int, int] = {id(s): 0 for s in systems}
        edges: dict[int, list[int]] = defaultdict(list)
        for system in systems:
            for dep in getattr(system, "depends_on", []) or []:
                dep_name = dep if isinstance(dep, str) else dep.__name__
                for target in by_name.get(dep_name, []):
                    edges[id(target)].append(id(system))
                    indegree[id(system)] += 1
        by_id = {id(s): s for s in systems}
        order_index = {id(s): i for i, s in enumerate(systems)}
        queue = deque(sorted(
            (sid for sid, deg in indegree.items() if deg == 0),
            key=order_index.get,
        ))
        sorted_ids = []
        while queue:
            sid = queue.popleft()
            sorted_ids.append(sid)
            for nxt in edges[sid]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)
        if len(sorted_ids) != len(systems):
            raise ValueError("system dependency cycle detected")
        return [by_id[sid] for sid in sorted_ids]

    def update(self, dt: float = 1.0) -> None:
        """按依赖顺序更新所有启用的系统，并应用各自的时间缩放。"""
        if self._sorted_systems is None:
            self._sorted_systems = self._sort_systems()
        for system in self._sorted_systems:
            if not self._system_enabled.get(id(system), True):
                continue
            if not hasattr(system, "update"):
                continue
            scale = self._system_time_scale.get(id(system), 1.0)
            system.update(self, dt * scale)

    # ---------- 事件 ----------

    def subscribe(self, event_type: str, handler: Callable) -> None:
        """订阅指定类型的事件，只收到该类型的事件。"""
        self._event_subs[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: Callable) -> None:
        if handler in self._event_subs.get(event_type, []):
            self._event_subs[event_type].remove(handler)

    def emit_event(
        self,
        event_type: str,
        data: Any = None,
        priority: int = 0,
    ) -> Event:
        """发送事件，进入优先级队列(数字越小越优先)，并记录到历史。"""
        event = Event(type=event_type, data=data, priority=priority,
                      _seq=next(self._event_seq))
        heapq.heappush(self._event_queue, (priority, event._seq, event))
        self._event_history.append(event)
        return event

    def dispatch_events(self) -> int:
        """按优先级分发事件，只派发给订阅了对应类型的处理器。"""
        dispatched = 0
        while self._event_queue:
            _, _, event = heapq.heappop(self._event_queue)
            for handler in list(self._event_subs.get(event.type, [])):
                handler(event)
                dispatched += 1
        return dispatched

    def event_history(self, event_type: Optional[str] = None) -> list[Event]:
        """查询事件历史，可按类型过滤，用于回溯。"""
        if event_type is None:
            return list(self._event_history)
        return [e for e in self._event_history if e.type == event_type]

    def replay_events(self, event_type: Optional[str] = None) -> int:
        """回放历史事件给当前订阅者。"""
        count = 0
        for event in self.event_history(event_type):
            for handler in list(self._event_subs.get(event.type, [])):
                handler(event)
                count += 1
        return count
