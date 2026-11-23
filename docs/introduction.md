# Introduction

## Purpose

Chongming tests whether several cameras can estimate the position and motion of a small object in the sky. It focuses on fixed stations and daylight images.

A camera shows where an object appears in an image. That image alone does not give a reliable distance. Cameras at different known positions provide different viewing directions. The center algorithm combines those directions to estimate a position, then uses later observations to form a track.

Each station has two cameras. The main camera watches moving targets. The auxiliary camera watches a fixed checkerboard reference to help estimate camera orientation.

## How experiments work

The system separates scene generation, online algorithms, routing, evaluation, and display into independent processes:

- **Godot Producer** (`godot_producer/`): generates 3D scenes, renders main/auxiliary images, computes 64-bit target truth and visibility, and optionally emits virtual rays.
- **Test Scene**: an immutable recording package produced by `V-Switch Record` from `Godot Producer` streams.
- **Edge & Center** (`chongming-edge`, `chongming-center-bus`): online algorithm processes that run on real or recorded camera streams and connect to `V-Switch` (`crates/vswitch/`) over TCP Protobuf.
- **Scorer** (`chongming-scorer`): compares online results against `Test Scene` truth offline or live via `V-Switch`.
- **Godot Viewer** (`src/chongming/rendering/godot/`): a passive 3D client connected to `V-Switch` (`--viewer`).

Three kinds of information stay distinct:

| Term        | Meaning                                                        |
| ----------- | -------------------------------------------------------------- |
| Scene truth | The target position and motion recorded in the `Test Scene`    |
| Measurement | A position estimated by the center algorithm from observations |
| Track       | An estimate of a target's motion over several observations     |

Truth is used only for evaluation and visualization overlays. Online algorithm interfaces never read target truth.

## What you can inspect

- Record deterministic multi-camera virtual test scenes using Godot Producer.
- Replay recorded `Test Scene` packages in Edge, Center, or Integrated modes over `V-Switch` (Lock-Step or Wall-Clock).
- Connect live cameras through `chongming-edge` -> `V-Switch Live` -> `chongming-center-bus`.
- Inspect live 3D rays, calibration states, measurements, tracks, raw voxels, and optional truth/score overlays in the passive Godot Viewer.
- Compute reproducible accuracy, timing, and attribution reports with `chongming-scorer`.

## Before use

The project requires technical setup. See [setup and first run](development.md#environment).

Scene recording and 3D viewing use Godot and require a graphics environment (or Xvfb software OpenGL on headless Linux). Video encoding and decoding require FFmpeg. The passive Godot Viewer labels are mainly in Chinese.

## Reading the display

In the passive Godot Viewer (`chongming-viewer-v1`), the 3D free view can toggle these diagnostic layers:

| Layer        | Meaning                                                                      |
| ------------ | ---------------------------------------------------------------------------- |
| Truth        | Green markers show recorded targets and FOV visibility (absent in `live`)    |
| Rays         | Yellow lines show viewing directions reported by devices                     |
| Measurements | Red markers show estimated positions and uncertainty bounds                  |
| Tracks       | Lines and markers show estimates over time; color indicates track state      |
| Voxels       | Grid cells show where rays from different stations support a possible target |

In free view, hold the right mouse button to look around. WASD moves horizontally, Q/E moves vertically, and Shift increases speed.

## Reading results

A visible target may produce no position estimate. Detection may fail, calibration may be unavailable, or too few stations may provide compatible observations.

`chongming-scorer` matches algorithm outputs against `Test Scene` truth at corresponding exposure midpoints, stratifying by pixel/model bounds and range. A low position error with low recall does not establish reliable detection.

## Limits and responsible use

The project does not identify target classes. Synthetic experiments do not establish real-world detection distance, accuracy, or reliable operation in weather and complex backgrounds.

Use only data and scenes that you have permission to use. Preserve attribution when required. Real cameras may capture people or private areas even when aimed at the sky. See the [license and use statement](../README.md#license-and-use).
