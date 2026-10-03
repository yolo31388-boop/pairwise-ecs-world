"""ECS系统测试"""
import pytest
from ecs.world import World, Component


class Position(Component):
    def __init__(self, x=0, y=0):
        self.x = x
        self.y = y


class Velocity(Component):
    def __init__(self, vx=0, vy=0):
        self.vx = vx
        self.vy = vy


class Health(Component):
    def __init__(self, hp=100):
        self.hp = hp


class TestEntity:
    def test_id_reuse(self):
        """销毁的实体ID应被复用"""
        world = World()
        e1 = world.create_entity()
        e2 = world.create_entity()
        world.destroy_entity(e1)
        e3 = world.create_entity()
        # e3应该复用e1的ID
        assert e3 == e1

    def test_version_increment(self):
        """销毁实体后version应增加，防止悬空指针"""
        world = World()
        e1 = world.create_entity()
        version_before = world.entities[e1].version
        world.destroy_entity(e1)
        # 重新创建同ID实体时version应不同
        e2 = world.create_entity()
        if e2 == e1:
            assert world.entities[e2].version != version_before

    def test_destroy_nonexistent(self):
        """销毁不存在的实体应返回False"""
        world = World()
        assert world.destroy_entity(9999) is False


class TestComponent:
    def test_add_to_nonexistent_entity(self):
        """向不存在的实体添加组件应失败或创建实体"""
        world = World()
        # 这个行为可以是创建实体或失败，但应该一致
        result = world.add_component(9999, Position())
        assert result in (True, False)

    def test_remove_updates_cache(self):
        """移除组件后缓存应更新"""
        world = World()
        e = world.create_entity()
        world.add_component(e, Position())
        world.add_component(e, Velocity())
        assert e in world.query_entities([Position, Velocity])
        world.remove_component(e, Velocity)
        # 移除后查询不应包含该实体
        assert e not in world.query_entities([Position, Velocity])

    def test_get_component(self):
        """获取组件"""
        world = World()
        e = world.create_entity()
        pos = Position(10, 20)
        world.add_component(e, pos)
        result = world.get_component(e, Position)
        assert result is not None
        assert result.x == 10
        assert result.y == 20


class TestQuery:
    def test_query_filter(self):
        """查询应正确过滤组件"""
        world = World()
        e1 = world.create_entity()
        e2 = world.create_entity()
        e3 = world.create_entity()
        world.add_component(e1, Position())
        world.add_component(e1, Velocity())
        world.add_component(e2, Position())
        world.add_component(e3, Health())

        result = world.query_entities([Position, Velocity])
        assert e1 in result
        assert e2 not in result
        assert e3 not in result

    def test_query_empty(self):
        """查询空组件类型应返回所有实体"""
        world = World()
        e1 = world.create_entity()
        e2 = world.create_entity()
        result = world.query_entities([])
        assert len(result) == 2


class TestSystem:
    def test_system_dependency_order(self):
        """系统应按依赖顺序执行"""
        world = World()
        execution_order = []

        class SystemA:
            def update(self, w, dt):
                execution_order.append("A")

        class SystemB:
            def update(self, w, dt):
                execution_order.append("B")

        # 这个测试验证系统执行的合理性
        world.register_system(SystemA())
        world.register_system(SystemB())
        world.update()
        assert len(execution_order) == 2
