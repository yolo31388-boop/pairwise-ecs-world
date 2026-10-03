"""ECS实体组件系统 - 存在bug"""
from dataclasses import dataclass, field
from typing import Any, Optional
import itertools


@dataclass
class Component:
    pass


@dataclass
class Entity:
    id: int
    version: int = 0
    components: dict[str, Component] = field(default_factory=dict)


class World:
    def __init__(self):
        self.entities: dict[int, Entity] = {}
        self._next_id = 1
        self._free_ids: list[int] = []
        self.systems: list = []
        self._component_cache: dict[str, set[int]] = {}

    def create_entity(self) -> int:
        """创建实体 - BUG: 不做ID复用"""
        # BUG: 不使用_free_ids，每次都新建ID
        entity_id = self._next_id
        self._next_id += 1
        self.entities[entity_id] = Entity(id=entity_id)
        return entity_id

    def destroy_entity(self, entity_id: int) -> bool:
        """销毁实体 - BUG: 不加入free列表，不增加version"""
        if entity_id not in self.entities:
            return False
        # BUG: 不加入_free_ids，不增加version
        del self.entities[entity_id]
        return True

    def add_component(self, entity_id: int, component: Component) -> bool:
        """添加组件 - BUG: 不检查实体是否存在"""
        # BUG: 不检查实体是否存在
        if entity_id not in self.entities:
            # BUG: 仍然创建实体并添加组件
            self.entities[entity_id] = Entity(id=entity_id)
        comp_name = type(component).__name__
        self.entities[entity_id].components[comp_name] = component
        # 更新缓存
        if comp_name not in self._component_cache:
            self._component_cache[comp_name] = set()
        self._component_cache[comp_name].add(entity_id)
        return True

    def get_component(self, entity_id: int, comp_type: type) -> Optional[Component]:
        """获取组件"""
        if entity_id not in self.entities:
            return None
        return self.entities[entity_id].components.get(comp_type.__name__)

    def remove_component(self, entity_id: int, comp_type: type) -> bool:
        """移除组件 - BUG: 不更新缓存"""
        if entity_id not in self.entities:
            return False
        comp_name = comp_type.__name__
        if comp_name in self.entities[entity_id].components:
            del self.entities[entity_id].components[comp_name]
            # BUG: 不更新_component_cache
            return True
        return False

    def query_entities(self, comp_types: list[type]) -> list[int]:
        """查询实体 - BUG: 只做简单遍历，不做位掩码过滤"""
        # BUG: 不使用缓存，每次都遍历所有实体
        result = []
        comp_names = [t.__name__ for t in comp_types]
        for entity_id, entity in self.entities.items():
            has_all = all(name in entity.components for name in comp_names)
            if has_all:
                result.append(entity_id)
        return result

    def register_system(self, system):
        """注册系统"""
        self.systems.append(system)

    def update(self, dt: float = 1.0):
        """更新所有系统 - BUG: 不做依赖排序"""
        # BUG: 不做依赖排序，按注册顺序执行
        for system in self.systems:
            if hasattr(system, "update"):
                system.update(self, dt)
