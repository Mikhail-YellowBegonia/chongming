#!/usr/bin/env python3
"""
验证飞机模型加载功能
"""

import json

from chongming.contracts.scene import ExternalAssetGeometry
from chongming.scenarios.authoring import compile_authoring

# 读取测试场景
with open("scenarios/aircraft_model_test.json") as f:
    spec = json.load(f)

# 构建场景
scenario = compile_authoring(spec)

# 验证默认飞机
flying_objects = {obj.display_name: obj for obj in scenario.scene_definition.flying_objects}

default = flying_objects["默认飞机"]
print(f"✓ 默认飞机加载: {default.display_name}")
print(f"  几何类型: {type(default.geometry).__name__}")

# 验证自定义模型飞机
f86 = flying_objects["F-86 Sabre"]
print(f"✓ F-86 加载: {f86.display_name}")
print(f"  几何类型: {type(f86.geometry).__name__}")

if isinstance(f86.geometry, ExternalAssetGeometry):
    print(f"  模型标识: {f86.geometry.asset_identifier}")
    assert f86.geometry.asset_identifier == "f86", "模型标识不匹配"
else:
    raise TypeError("F-86 应该使用 ExternalAssetGeometry")

print("\n✓ 所有验证通过！")
