# chongming

Chongming is a Python, Rust, and Godot project for testing how fixed camera stations detect, locate, and track small airborne targets in daylight.

It provides a standalone Godot virtual scene producer, online edge and center bus processes, a Rust `V-Switch` hub, an independent scorer, and a passive 3D viewer. It is a research tool, not a validated field system.

## How it works

```text
Godot Producer (godot_producer/)
  -> V-Switch Record (crates/vswitch/) -> immutable Test Scene (.pb.zst + MP4)

Test Scene or live cameras
  -> Edge process (chongming-edge) -> V-Switch Relay -> Center process (chongming-center-bus)
  -> V-Switch -> Scorer (chongming-scorer) + passive Godot Viewer (--viewer)
```

Virtual scene truth and sensor rendering live in `godot_producer/` and `Test Scene` packages. Online algorithm packages (`chongming`, `chongming-edge`, `chongming-center-bus`) consume ordinary frames, policies, and online events without importing truth schemas.

Current code includes frame-difference and MOG2 detection (plus optional external ViBe), checkerboard auxiliary calibration, voxel candidate search, weighted position estimation, constant-velocity tracking, Lock-Step/Wall-Clock `V-Switch` replay, and independent size/range-aware scoring.

## Documentation

| Read                                 | Purpose                                                              |
| ------------------------------------ | -------------------------------------------------------------------- |
| [Introduction](docs/introduction.md) | Understand the project, components, and result limits                |
| [Design](docs/design.md)             | Study the architecture, data contracts, and algorithms               |
| [Development](docs/development.md)   | Install, record/replay scenes, run checks, and build local releases  |
| [Wire Protocol](docs/protocol.md)    | Study the Protobuf bus, `.pb.zst` storage, and `chongming-viewer-v1` |
| [Agent instructions](AGENTS.md)      | Follow repository rules when changing files                          |

## Start

Use the repository's Nix and uv environment. Godot 4.7.2 stable and an OpenGL 3.3 graphics context are required for virtual scene recording (`godot_producer/`) and passive visualization (`Godot Viewer`). FFmpeg is required for video encoding and playback. Setup commands, software rendering, and release installation are in [Development](docs/development.md#environment).

After setup, build `vswitch` and run a smoke check:

```sh
cargo build -p vswitch
uv run --frozen chongming-center-bus --help
uv run --frozen chongming-edge --help
uv run --frozen chongming-scorer --help
```

## License and use

Code is licensed under the GNU Affero General Public License v3.0. See [COPYING](COPYING). Dependencies, maps, elevation data, and external models retain their own licenses. See [asset sources and terms](docs/development.md#third-party-assets-and-licenses).

Except when otherwise stated in writing the copyright holders and/or other parties provide the program "as is" without warranty of any kind, either expressed or implied, including, but not limited to, the implied warranties of merchantability and fitness for a particular purpose. The entire risk as to the quality and performance of the program is with you. Should the program prove defective, you assume the cost of all necessary servicing, repair or correction.

You decide for yourself whether to use this project and for what purpose, and you assume full responsibility and all risks arising from such use. The authors and contributors shall not be held liable for any systems developed, deployed, or operated by anyone using this project, or any modified or derivative versions thereof, nor for any direct or indirect losses resulting from such use.

This project may not be used for any purpose that violates the laws or regulations of the country or region in which it is used, including but not limited to military activities, surveillance, or the infringement of individual privacy. The user assumes full legal responsibility for any unlawful or non-compliant actions taken using this project, the authors and contributors of this project bear no liability for such actions.
