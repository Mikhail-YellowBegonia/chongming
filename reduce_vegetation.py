#!/usr/bin/env python3
"""快速生成减少植被的场景变体"""
import json
import sys
from pathlib import Path

def reduce_vegetation(input_file: str, output_file: str, reduction_factor: float = 0.5):
    """
    减少场景中的植被密度

    Args:
        input_file: 输入场景 JSON
        output_file: 输出场景 JSON
        reduction_factor: 保留比例 (0.5 = 保留50%, 砍掉50%)
    """
    with open(input_file) as f:
        scene = json.load(f)

    # 修改 OSM 植被配置
    if 'osm' in scene and 'vegetation' in scene['osm']:
        veg = scene['osm']['vegetation']

        # 减少各类植被密度
        density_keys = [
            'forest_tree_density_per_ha',
            'grass_tree_density_per_ha',
            'forest_shrub_density_per_ha',
            'grass_shrub_density_per_ha',
            'forest_grass_density_per_ha',
            'grass_grass_density_per_ha',
        ]

        for key in density_keys:
            if key in veg:
                original = veg[key]
                veg[key] = original * reduction_factor
                print(f"  {key}: {original:.1f} → {veg[key]:.1f} ({reduction_factor*100:.0f}%)")

    # 更新 scenario_id
    if 'scenario_id' in scene:
        scene['scenario_id'] = f"{scene['scenario_id']}-reduced{int(reduction_factor*100)}"

    # 保存
    with open(output_file, 'w') as f:
        json.dump(scene, f, indent=2)

    print(f"\n✅ 已生成减少植被场景: {output_file}")
    print(f"   植被保留比例: {reduction_factor*100:.0f}%")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: python3 reduce_vegetation.py <场景文件> [保留比例]")
        print("示例: python3 reduce_vegetation.py scenarios/hannover_probe_5s.json 0.5")
        sys.exit(1)

    input_file = sys.argv[1]
    reduction = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5

    # 生成输出文件名
    p = Path(input_file)
    output_file = p.parent / f"{p.stem}_reduced{int(reduction*100)}{p.suffix}"

    print(f"减少植被密度...")
    print(f"输入: {input_file}")
    print(f"输出: {output_file}")
    print(f"保留比例: {reduction*100:.0f}%")
    print()

    reduce_vegetation(input_file, str(output_file), reduction)
