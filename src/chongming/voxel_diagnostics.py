"""Opt-in raw unguided votes for the reference center's current invocation."""

from __future__ import annotations

from dataclasses import dataclass

from chongming.contracts import fusion as f
from chongming.contracts import model as m
from chongming.contracts.visualization import ScoredVoxel, VoxelSpaceVisualization


@dataclass(kw_only=True)
class VoxelDiagnostics:
    """Keep at most one snapshot; never feed display data back into search."""

    enabled: bool = False
    window: f.WindowKey | None = None
    snapshot: VoxelSpaceVisualization | None = None

    def clear(self) -> None:
        self.window = None
        self.snapshot = None

    def capture(
        self,
        window: f.WindowKey,
        search: m.VoxelSearchSpec,
        cells: dict[tuple[int, int, int], dict[m.StationId, set[m.EventId]]],
    ) -> None:
        if not self.enabled:
            return
        # The search's score is independent-station count, not ray multiplicity
        # or a detection probability. Include cells below candidate thresholds.
        voxels = tuple(
            ScoredVoxel(
                grid_index=index, score=len(stations), supporting_station_count=len(stations)
            )
            for index, stations in sorted(cells.items())
            if stations
        )
        self.window = window
        self.snapshot = VoxelSpaceVisualization(
            search_box_min_enu_m=search.min_enu_m,
            search_box_max_enu_m=search.max_enu_m,
            voxel_step_enu_m=search.step_enu_m,
            scored_voxels=voxels,
            max_score=max((voxel.score for voxel in voxels), default=0),
        )
