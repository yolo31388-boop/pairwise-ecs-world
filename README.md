# ECS实体组件系统

## 项目结构
- `ecs/world.py`: 核心实现
- `tests/test_ecs.py`: 验收测试

## 运行测试
```bash
python -m pytest tests/test_ecs.py -q
```

## 能力概览

### 实体
- `create_entity()`：优先复用空闲ID，版本号随销毁递增，旧引用自然失效
- `create_entities(n)`：批量创建实体
- `destroy_entity(id)`：ID回收到最小堆，版本号+1，同步清理组件与查询缓存
- `is_alive(id)` / `get_version(id)`：存活与版本检查

### 组件
- `add_component()`：实体不存在（含已销毁）时拒绝，返回 `False`
- 新组件类型首次使用时自动注册，无需手动维护枚举（`registry.register()` / `registry.bit()`）
- 同类型组件连续存储，按64字节缓存行对齐分块（`ComponentStorage.aligned_batches()`），便于SIMD批处理

### 查询
- `query_entities([...])`：组件位掩码过滤
- 查询结果按组件类型集合缓存
- 组件增删、实体创建/销毁时增量更新缓存，无需每帧重算

### 系统
- 系统通过 `dependencies` / `after` 声明依赖，更新前拓扑排序（检测循环依赖）
- `enable_system()` / `disable_system()`，也可直接设置 `system.enabled`
- `world.time_scale` 与 `system.time_scale` 共同缩放传入的 `dt`

### 事件
- `emit_event(event, priority=0)`：按优先级处理（数值越大越先处理）
- `subscribe(type, handler, filter=None)`：`filter(event)` 返回 False 时不投递
- 所有事件记录到 `event_history`，`get_event_history(type=None)` 支持回溯
