# Introduction

## Purpose

Chongming tests whether several cameras can estimate the position and motion of a small object in the sky. It focuses on fixed stations and daylight images.

A camera shows where an object appears in an image. That image alone does not give a reliable distance. Cameras at different known positions provide different viewing directions. The center algorithm combines those directions to estimate a position, then uses later observations to form a track.

Each station has two cameras. The main camera watches moving targets. The auxiliary camera watches a fixed reference to help estimate camera orientation.

## What the experiment contains

The project creates a scene with cameras, targets, terrain, and optional vegetation. It generates images and runs algorithms on them. It can also test the center algorithm with simulated viewing directions, without running image detection.

Three kinds of information must stay distinct:

| Term        | Meaning                                                        |
| ----------- | -------------------------------------------------------------- |
| Scene truth | The target position and motion known to the simulator          |
| Measurement | A position estimated by the algorithm from camera observations |
| Track       | An estimate of a target's motion over several observations     |

Truth is a reference for checking results. The algorithm does not receive the simulator's target positions through its normal input.

## What you can inspect

- Preview a scene and view its main and auxiliary camera images.
- Move a free-view camera to understand station and target positions.
- Run a device test, a center test, or an integrated test.
- Compare reported positions with scene truth.
- Edit supported scene settings and save an experiment record.

Preview mode has playback and editing controls. Algorithm test windows allow inspection but disable scene editing and time jumps. A predicted scene path is a preview of configured motion.

## Before use

The project requires technical setup. See [setup and first run](development.md#environment).

The display uses Godot and needs a graphics environment. Video recording also needs FFmpeg. Basic examples are included; some map scenes require separate data. The interface is mainly Chinese.

There is no guaranteed frame rate. A slow computer can take longer than the simulated duration to finish an experiment. Developers must check image correctness before using image-based results.

## Reading the display

The main and auxiliary views show sensor images. Free view shows the scene from a movable camera. It can add these diagnostic layers:

| Layer        | Meaning                                                                      |
| ------------ | ---------------------------------------------------------------------------- |
| Truth        | Green markers show targets known to the simulator                            |
| Rays         | Yellow lines show viewing directions reported by devices                     |
| Measurements | Red markers show estimated positions                                         |
| Tracks       | Lines and markers show estimates over time; color indicates track state      |
| Voxels       | Grid cells show where rays from different stations support a possible target |

Grid cells are search aids, a ray is a direction. Selecting a measurement can highlight its supporting rays if they are still in the display cache.

In a center-only test, virtual input rays are not displayed as device output. A position may therefore have no visible supporting ray.

Use the station selector to change sensor views. In free view, hold the right mouse button to look around. WASD moves horizontally, Q/E moves vertically, and Shift increases speed. Detailed controls are in [Development](development.md#preview-controls).

## Reading results

A visible target may produce no position estimate. Detection may fail, calibration may be unavailable, or too few stations may provide compatible observations.

The error panel compares algorithm output with truth at the corresponding time. It reports matched, unmatched, and unevaluable output. Mean error and root mean square error (RMSE) use matched results only.

A low error with few matches does not establish good overall detection. An unmatched result is not automatically a false alarm.

Track display can continue briefly without a fresh observation. This state is called coasting.

## Limits and responsible use

The project does not identify target classes. Synthetic experiments do not establish real-world detection distance, accuracy, or reliable operation in weather and complex backgrounds.

The error panel is not a full score of detection rate or track quality. It does not measure every missed target or identity change. Recorded preview is also limited.

Use only data and scenes that you have permission to use. Preserve map and model attribution when required. Real cameras may capture people or private areas, even if the experiment's target is in the sky. See the [license and use statement](../README.md#license-and-use).
