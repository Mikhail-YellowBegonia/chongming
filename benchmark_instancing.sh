#!/usr/bin/env bash
# Performance benchmark for GPU instancing
# Tests hannover_multi_target.json with 3 stations and 3796 trees

set -e

echo "=========================================="
echo "GPU Instancing Performance Benchmark"
echo "=========================================="
echo ""
echo "Scene: hannover_multi_target.json"
echo "Stations: 3"
echo "Trees: ~3796"
echo "Duration: 60s @ 30fps"
echo ""

# Check if in nix shell
if [ -z "$CHONGMING_GODOT" ]; then
    echo "❌ Not in nix shell. Please run:"
    echo "   nix develop --no-update-lock-file"
    exit 1
fi

echo "✅ Nix environment detected"
echo ""

# Test with instancing enabled (default)
echo "=========================================="
echo "Test 1: GPU Instancing ENABLED"
echo "=========================================="
echo ""
echo "Running preview with GPU instancing..."
echo "Watch Godot window for:"
echo "- Trees rendering correctly"
echo "- Wind animation (4.5 m/s)"
echo "- Framerate (target: 30+ fps)"
echo ""
echo "Press Ctrl+C to stop when you've observed the framerate"
echo ""

uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json

echo ""
echo "=========================================="
echo "Benchmark Complete"
echo "=========================================="
echo ""
echo "Please record the observed framerate and report:"
echo "- FPS during preview"
echo "- Any visual issues"
echo "- Godot console errors (if any)"
echo ""
