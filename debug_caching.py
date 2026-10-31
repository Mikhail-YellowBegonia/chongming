#!/usr/bin/env python3
"""
Debug script to check if static scene caching is working
"""
import sys
sys.path.insert(0, 'src')

from chongming.rendering.godot import _separate_static_dynamic_entities, _compute_static_scene_hash

# Test data
test_entities = [
    {"object_id": "veg_001", "category": "vegetation"},
    {"object_id": "veg_002", "category": "vegetation"},
    {"object_id": "terrain_001", "category": "terrain"},
    {"object_id": "aircraft/1", "category": "flying"},
    {"object_id": "aircraft/2", "category": "flying"},
]

static, dynamic = _separate_static_dynamic_entities(test_entities)

print(f"Total entities: {len(test_entities)}")
print(f"Static entities: {len(static)}")
print(f"Dynamic entities: {len(dynamic)}")
print()
print("Static IDs:", [e["object_id"] for e in static])
print("Dynamic IDs:", [e["object_id"] for e in dynamic])
print()

# Test hash
hash1 = _compute_static_scene_hash(static)
print(f"Static scene hash: {hash1}")

# Add another entity, hash should change
static2 = static + [{"object_id": "building_001", "category": "building"}]
hash2 = _compute_static_scene_hash(static2)
print(f"Modified hash: {hash2}")
print(f"Hashes different: {hash1 != hash2}")
