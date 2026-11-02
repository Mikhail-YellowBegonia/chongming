# chongming

Chongming is a Python and Godot project for testing how fixed camera stations detect, locate, and track small airborne targets in daylight.

It provides synthetic scenes, reference algorithms, and a result display. It is a research tool, not a validated field system.

## How it works

```text
Main camera images + auxiliary reference images
  -> detection and camera calibration
  -> world rays from each station
  -> center position estimates and tracks

Algorithm results + separate scene truth -> Godot display and error diagnostics
```

A center-only test can use virtual rays instead of images. Truth data stays outside online algorithm interfaces.

The project has scene preview, device tests, center tests, and integrated tests. Current code includes MOG2 detection, checkerboard calibration, voxel-based candidate search, weighted position estimation, and constant-velocity tracking. General station transport and system orchestration remain incomplete. See [implementation limits](docs/design.md#current-limits), including unresolved rendering paths.

## Documentation

| Read                                 | Purpose                                                                 |
| ------------------------------------ | ----------------------------------------------------------------------- |
| [Introduction](docs/introduction.md) | Understand the project, display, and result limits without reading code |
| [Design](docs/design.md)             | Study the architecture, data contracts, and algorithms                  |
| [Development](docs/development.md)   | Install, run, configure, test, and build the project                    |
| [Agent instructions](AGENTS.md)      | Follow repository rules when changing files                             |

## Start

Use the repository's Nix and uv environment. Actual runs require Godot 4.7.2 stable and a graphics session with OpenGL 3.3. FFmpeg is required for video recording and playback. Setup commands, software rendering, and release installation are in [Development](docs/development.md#environment).

After setup, open a bundled preview:

```sh
uv run --frozen chongming-preview --scenario scenarios/review.json
```

Map assets for external scenarios must be prepared separately.

## License and use

Code is licensed under the GNU Affero General Public License v3.0. See [COPYING](COPYING). Dependencies, maps, elevation data, and models retain their own licenses. [Asset sources and terms](docs/development.md#third-party-assets-and-licenses) include the bundled CC0 plant kit.

Except when otherwise stated in writing the copyright holders and/or other parties provide the program "as is" without warranty of any kind, either expressed or implied, including, but not limited to, the implied warranties of merchantability and fitness for a particular purpose. The entire risk as to the quality and performance of the program is with you. Should the program prove defective, you assume the cost of all necessary servicing, repair or correction.

You decide for yourself whether to use this project and for what purpose, and you assume full responsibility and all risks arising from such use. The authors and contributors shall not be held liable for any systems developed, deployed, or operated by anyone using this project, or any modified or derivative versions thereof, nor for any direct or indirect losses resulting from such use.

This project may not be used for any purpose that violates the laws or regulations of the country or region in which it is used, including but not limited to military activities, surveillance, or the infringement of individual privacy. The user assumes full legal responsibility for any unlawful or non-compliant actions taken using this project, the authors and contributors of this project bear no liability for such actions.
