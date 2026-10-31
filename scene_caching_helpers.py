def _compute_static_scene_hash(entities: list[dict[str, Any]]) -> str:
    """Compute a hash of static entities (vegetation, terrain, buildings).

    This is used to detect if the static scene has changed and needs to be resent.
    Only includes entities that don't move (excludes flying objects and cameras).
    """
    import hashlib

    # Filter to static entities only
    static_entities = []
    for entity in entities:
        category = entity.get("category", "")
        # Dynamic objects: flying objects (aircraft)
        # Static objects: vegetation, terrain, buildings, world objects
        if not entity.get("object_id", "").startswith("aircraft/"):
            static_entities.append(entity)

    # Compute hash of static entities
    # Use a stable serialization
    import json
    serialized = json.dumps(static_entities, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _separate_static_dynamic_entities(
    entities: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate entities into static and dynamic groups.

    Returns:
        (static_entities, dynamic_entities)

    Static: vegetation, terrain, buildings (don't move)
    Dynamic: aircraft (move every frame)
    """
    static = []
    dynamic = []

    for entity in entities:
        object_id = entity.get("object_id", "")
        # Aircraft are dynamic
        if object_id.startswith("aircraft/"):
            dynamic.append(entity)
        else:
            # Everything else (vegetation, terrain, buildings) is static
            static.append(entity)

    return static, dynamic
