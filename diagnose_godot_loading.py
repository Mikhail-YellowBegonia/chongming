#!/usr/bin/env python3
"""诊断 Godot 脚本加载问题的调试脚本"""

import os
import sys
from importlib.resources import as_file, files
from pathlib import Path


def main():
    print("=" * 80)
    print("Godot 脚本加载诊断")
    print("=" * 80)

    # 1. 检查包安装模式
    print("\n1. 包安装信息:")
    try:
        import chongming
        print(f"   chongming 包路径: {chongming.__file__}")
        print(f"   chongming.__path__: {chongming.__path__}")
    except ImportError as e:
        print(f"   ❌ 无法导入 chongming: {e}")
        return 1

    # 2. 检查 rendering 模块
    print("\n2. rendering 模块:")
    try:
        import chongming.rendering
        print(f"   rendering.__file__: {chongming.rendering.__file__}")
        print(f"   rendering.__path__: {chongming.rendering.__path__}")
    except ImportError as e:
        print(f"   ❌ 无法导入 chongming.rendering: {e}")
        return 1

    # 3. 检查 importlib.resources 路径
    print("\n3. importlib.resources 解析:")
    godot_resources = files("chongming.rendering").joinpath("godot")
    print(f"   资源对象: {godot_resources}")
    print(f"   资源类型: {type(godot_resources)}")

    # 4. 使用 as_file() 获取实际路径
    print("\n4. as_file() 返回的路径:")
    with as_file(godot_resources) as godot_path:
        print(f"   路径: {godot_path}")
        print(f"   类型: {type(godot_path)}")
        print(f"   存在: {godot_path.exists()}")
        print(f"   是目录: {godot_path.is_dir()}")
        print(f"   解析后: {godot_path.resolve()}")

        # 5. 检查 worker.gd
        print("\n5. worker.gd 检查:")
        worker_gd = godot_path / "worker.gd"
        print(f"   路径: {worker_gd}")
        print(f"   存在: {worker_gd.exists()}")

        if worker_gd.exists():
            with open(worker_gd, 'r') as f:
                lines = f.readlines()
                print(f"   文件大小: {worker_gd.stat().st_size} 字节")
                print(f"   修改时间: {worker_gd.stat().st_mtime}")

                # 查找调试标记
                print("\n   查找调试标记 'GPU INSTANCING':")
                found = False
                for i, line in enumerate(lines, 1):
                    if "GPU INSTANCING" in line:
                        print(f"     ✅ 第 {i} 行: {line.strip()}")
                        found = True

                if not found:
                    print("     ❌ 未找到 'GPU INSTANCING' 标记")

                # 显示前 10 行
                print("\n   前 10 行:")
                for i, line in enumerate(lines[:10], 1):
                    print(f"     {i:3d}: {line.rstrip()}")

    # 6. 检查源文件
    print("\n6. 源文件检查:")
    src_worker = Path(__file__).parent / "src/chongming/rendering/godot/worker.gd"
    print(f"   路径: {src_worker}")
    print(f"   存在: {src_worker.exists()}")

    if src_worker.exists():
        with open(src_worker, 'r') as f:
            lines = f.readlines()
            print(f"   文件大小: {src_worker.stat().st_size} 字节")
            print(f"   修改时间: {src_worker.stat().st_mtime}")

            print("\n   查找调试标记 'GPU INSTANCING':")
            found = False
            for i, line in enumerate(lines, 1):
                if "GPU INSTANCING" in line:
                    print(f"     ✅ 第 {i} 行: {line.strip()}")
                    found = True

            if not found:
                print("     ❌ 未找到 'GPU INSTANCING' 标记")

    # 7. 检查 Godot 缓存
    print("\n7. Godot 缓存检查:")
    home = Path.home()
    godot_cache_locations = [
        home / ".local/share/godot",
        home / "Library/Application Support/Godot",
        home / "AppData/Roaming/Godot",
    ]

    for cache_dir in godot_cache_locations:
        if cache_dir.exists():
            print(f"   找到缓存: {cache_dir}")
            # 查找可能相关的缓存
            cache_files = list(cache_dir.rglob("chongming*"))
            if cache_files:
                print(f"     相关文件: {len(cache_files)} 个")
                for f in cache_files[:5]:
                    print(f"       - {f.relative_to(cache_dir)}")

    # 8. 检查临时目录
    print("\n8. 临时目录检查:")
    import tempfile
    temp_dir = Path(tempfile.gettempdir())
    print(f"   临时目录: {temp_dir}")
    chongming_temps = list(temp_dir.glob("chongming-*"))
    print(f"   chongming-* 目录数: {len(chongming_temps)}")

    if chongming_temps:
        print("   最近的临时目录:")
        for tmp in sorted(chongming_temps, key=lambda p: p.stat().st_mtime, reverse=True)[:3]:
            print(f"     - {tmp.name}")
            worker_in_tmp = tmp / "project/worker.gd"
            if worker_in_tmp.exists():
                print(f"       包含 worker.gd (大小: {worker_in_tmp.stat().st_size})")

    print("\n" + "=" * 80)
    print("诊断完成")
    print("=" * 80)

    return 0


if __name__ == "__main__":
    sys.exit(main())
