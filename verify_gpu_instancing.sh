#!/usr/bin/env bash
# 验证 GPU Instancing 功能的脚本

set -e

echo "=========================================="
echo "GPU Instancing 功能验证"
echo "=========================================="

cd "$(dirname "$0")"

echo ""
echo "1. 检查源码中的调试标记..."
if grep -q "GPU INSTANCING" src/chongming/rendering/godot/worker.gd; then
    echo "   ✅ 源码包含调试标记"
    grep -n "GPU INSTANCING" src/chongming/rendering/godot/worker.gd | head -3
else
    echo "   ❌ 源码缺少调试标记"
    exit 1
fi

echo ""
echo "2. 运行诊断脚本..."
uv run python diagnose_godot_loading.py | grep -A5 "GPU INSTANCING"

echo ""
echo "3. 启动预览窗口..."
echo "   请手动执行以下命令测试："
echo ""
echo "   uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json"
echo ""
echo "=========================================="
echo "验证检查项："
echo "=========================================="
echo ""
echo "✅ 窗口标题应显示: Chongming 预览 [GPU INSTANCING]"
echo "✅ 左上角 FPS 显示应包含: GPU: X inst / Y batch"
echo "✅ 性能应有明显提升（相比之前的实现）"
echo ""
echo "如果看到以上三项，说明 GPU Instancing 功能正常工作。"
echo ""
echo "=========================================="
echo "Godot 日志位置："
echo "=========================================="
echo ""
echo "~/Library/Application Support/Godot/app_userdata/Chongming Virtual Scene Renderer/logs/godot.log"
echo ""
echo "查看最新日志："
echo "tail -50 ~/Library/Application\\ Support/Godot/app_userdata/Chongming\\ Virtual\\ Scene\\ Renderer/logs/godot.log"
echo ""
