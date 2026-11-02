# Agent instructions

## Read by task

Read the relevant sections before changing files. Do not read every document for every task.

| Task                                    | Source                                                                                                                                                          |
| --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Project purpose and limits              | [README](README.md), [Introduction](docs/introduction.md)                                                                                                       |
| Architecture, ownership, and data rules | [Design](docs/design.md)                                                                                                                                        |
| Environment and dependencies            | [Development: environment](docs/development.md#environment)                                                                                                     |
| Commands, scenes, and device policies   | [Development: commands](docs/development.md#commands-and-modes), [scenarios](docs/development.md#scenarios), [device policy](docs/development.md#device-policy) |
| Tests and formatting                    | [Development: checks](docs/development.md#checks)                                                                                                               |
| Builds and release validation           | [Development: local release](docs/development.md#local-release)                                                                                                 |
| Visualization and error metrics         | [Design: rendering and display](docs/design.md#rendering-and-display), [visualization contracts](src/chongming/contracts/visualization.py)                      |
| Asset sources and permissions           | [README: license](README.md#license-and-use), [asset terms](docs/development.md#third-party-assets-and-licenses)                                                |

## Before editing

- Check the branch and working tree. Preserve user changes and unrelated files.
- Confirm scope or design choices when they affect implementation.
- Use the repository's Nix environment and uv workflow. Respect both lock files.
- Do not install into the system environment. Do not add or upgrade dependencies, refresh locks, or replace tools without explicit approval.

## Implementation

- Solve the current requirement with simple code and existing module boundaries.
- Do not add general frameworks, configuration layers, compatibility layers, retries, or fallbacks for hypothetical needs.
- Validate external input and data integrity at boundaries. Avoid repeated internal checks. Do not swallow exceptions or hide invalid data with defaults.
- Keep generation truth out of online algorithm interfaces. Do not replace failed image processing with virtual observations.
- Report unimplemented work as unimplemented. Initialization is not proof that an algorithm works.
- Keep code, identifiers, and comments in English.

## Verification

- Add tests for behavior changes. Run relevant checks; use full regression for shared contracts, runtime, or packaging changes.
- Do not weaken assertions, relax thresholds, or fabricate output to make tests pass.
- Report what ran, what failed, and what remains untested. Separate current results from historical evidence.
- Preserve existing experiment results. Use separate output directories for new runs.

## Documentation and delivery

- Use simple technical English in documentation. Prefer short sentences and consistent terms. Remove repetition, promotional claims, and unrelated material.
- Update the relevant guide when user-visible behavior or a contract changes. Maintain each technical rule in one main location; keep this file for navigation and constraints.
- Separate implemented behavior, measured results, and proposed work. Give performance claims a reproducible test context.
- Keep local assets, generated output, and secrets out of version control and release archives unless their inclusion has explicit approval and valid permissions. Never expose secrets.
- Do not stage, commit, push, or publish without explicit authorization. Report changes and verification, then wait for the maintainer.
