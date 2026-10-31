#!/usr/bin/env bash
# Force clean restart for GPU instancing test

set -e

echo "=========================================="
echo "强制清理并重启测试"
echo "=========================================="
echo ""

# 1. Kill any running Godot processes
echo "1. 关闭所有 Godot 进程..."
pkill -9 Godot 2>/dev/null || true
pkill -9 godot 2>/dev/null || true
sleep 1

# 2. Clear Godot caches
echo "2. 清理 Godot 缓存..."
rm -rf ~/.cache/godot/ 2>/dev/null || true
rm -rf ~/Library/Caches/Godot/ 2>/dev/null || true

# 3. Clear temp directories
echo "3. 清理临时目录..."
rm -rf /tmp/chongming-* 2>/dev/null || true

# 4. Verify source file
echo "4. 验证源文件..."
if grep -q "GPU INSTANCING" src/chongming/rendering/godot/worker.gd; then
    echo "   ✅ 源文件包含 GPU INSTANCING 代码"
else
    echo "   ❌ 源文件不包含 GPU INSTANCING 代码"
    exit 1
fi

echo ""
echo "清理完成！现在运行测试..."
echo ""

# 5. Run test
uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json
