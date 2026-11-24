"""Typed module implementations and the remaining architecture declarations."""

from .device import (
    AuxiliaryCalibrationModule,
    CalibrationEstimationModule,
    CalibrationHistoryModule,
    CalibrationStateModule,
    DeviceModule,
    EventSequencerModule,
    MainDetectionModule,
    MotionDetectionModule,
    RayGenerationModule,
    ReferenceRecognitionModule,
    Tracking2DModule,
)
from .fusion import (
    CalibrationWeightingModule,
    DirectGeometryCandidateModule,
)
from .fusion_association import CrossStationAssociationModule
from .fusion_engine import CenterFusionModule
from .fusion_geometry import (
    CandidateGenerationModule,
    GeometryRefinementModule,
    VoxelCandidateModule,
)
from .fusion_ingress import (
    AdmissionJournalModule,
    CalibrationRegistryModule,
    EventAdmissionModule,
    EventClockMappingModule,
    EventWindowModule,
    StationRegistryModule,
)
from .fusion_tracking import CenterTrackingModule

__all__ = [
    "AdmissionJournalModule",
    "AuxiliaryCalibrationModule",
    "CalibrationEstimationModule",
    "CalibrationHistoryModule",
    "CalibrationRegistryModule",
    "CalibrationStateModule",
    "CalibrationWeightingModule",
    "CandidateGenerationModule",
    "CenterFusionModule",
    "CenterTrackingModule",
    "CrossStationAssociationModule",
    "DeviceModule",
    "DirectGeometryCandidateModule",
    "EventAdmissionModule",
    "EventClockMappingModule",
    "EventSequencerModule",
    "EventWindowModule",
    "GeometryRefinementModule",
    "MainDetectionModule",
    "MotionDetectionModule",
    "RayGenerationModule",
    "ReferenceRecognitionModule",
    "StationRegistryModule",
    "Tracking2DModule",
    "VoxelCandidateModule",
]
