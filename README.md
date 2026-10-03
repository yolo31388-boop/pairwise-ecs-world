# ECS实体组件系统

## 项目结构
- `ecs/world.py`: 核心实现
- `tests/test_ecs.py`: 验收测试

## 运行测试
```bash
python -m pytest tests/test_ecs.py -q
```

## 能力一览
- **实体**：`create_entity()` 复用已销毁的ID；`EntityRef(id, version)` 带版本号，
  悬空引用经 `resolve()` 抛 `StaleEntityError`；`create_entities(n)` 批量创建。
- **组件**：`Component` 子类经元类自动注册到 `ComponentRegistry`（无需手改枚举）；
  `add_component()` 校验实体存活，向已销毁实体添加返回 `False`；
  `ComponentStore` 使用64字节对齐缓冲区（`is_aligned` / `data_address`），便于SIMD。
- **查询**：`query_entities()` 用组件位掩码过滤，结果按类型组合缓存，
  组件增删/实体销毁时自动失效并重算（增量更新）。
- **系统**：继承 `System`，用 `depends_on` 声明依赖，`update()` 前做拓扑排序；
  `enable_system/disable_system` 控制启停；`set_time_scale()` 做慢动作时间缩放。
- **事件**：`emit_event(type, data, priority)` 按优先级分发，
  `subscribe(type, handler)` 按类型过滤，历史可经 `event_history()` 回溯、`replay_events()` 回放。
