{
  description = "Chongming development environment and virtual-scene tools";

  inputs.nixpkgs.url = "https://channels.nixos.org/nixos-unstable/nixexprs.tar.zst";

  outputs =
    { nixpkgs, ... }:
    let
      inherit (nixpkgs) lib;

      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];

      pkgsFor = system: nixpkgs.legacyPackages.${system};

      pythonEnv = pkgs: {
        LD_LIBRARY_PATH = lib.makeLibraryPath [
          pkgs.stdenv.cc.cc.lib
          pkgs.zlib
        ];
        UV_PYTHON_DOWNLOADS = "never";
      };

      runtimeEnv =
        pkgs:
        pythonEnv pkgs
        // {
          CHONGMING_GODOT = lib.getExe pkgs.godot_4_7;
          CHONGMING_FFMPEG = lib.getExe pkgs.ffmpeg;
        };

      treefmtFor =
        pkgs:
        pkgs.writeShellApplication {
          name = "treefmt";
          runtimeInputs = [
            pkgs.nixfmt
            pkgs.prettier
            pkgs.ruff
            pkgs.rustfmt
            pkgs.git
          ];
          text = ''
            exec ${lib.getExe pkgs.treefmt} --config-file ${./treefmt.toml} "$@"
          '';
          meta.description = "Format the project with nixfmt, Ruff, rustfmt and Prettier";
        };

      # Test-scene assets that are not in Git. Each archive is produced by
      # scripts/pack_local_asset.py and attached to a GitHub prerelease; Nix
      # pins the bytes. See docs/development.md#fixed-test-assets.
      localAssetsFor =
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          hannover-500m = pkgs.fetchurl {
            name = "hannover-500m-2026-10-28.tar.gz";
            url = "https://github.com/Mikhail-YellowBegonia/chongming/releases/download/assets-hannover-500m-2026-10-28/hannover-500m-2026-10-28.tar.gz";
            sha256 = "cdb847e50f59757a29de04f4a1f05879322e76b8164402b7b394829c78511510";
            # OSM content is ODbL; DEM provider terms are recorded in the archive README.
            meta.license = [ lib.licenses.odbl ];
          };
        };

      appsFor =
        system:
        let
          pkgs = pkgsFor system;
          mkPyApp = name: {
            type = "app";
            program = lib.getExe (
              pkgs.writeShellApplication {
                inherit name;
                runtimeInputs = [
                  pkgs.python312
                  pkgs.uv
                  pkgs.ffmpeg
                ];
                runtimeEnv = runtimeEnv pkgs;
                text = ''
                  exec uv run --frozen ${name} "$@"
                '';
              }
            );
          };
        in
        {
          default = mkPyApp "chongming-center-bus";
          center-bus = mkPyApp "chongming-center-bus";
          edge = mkPyApp "chongming-edge";
          scorer = mkPyApp "chongming-scorer";
        };

      devShellsFor =
        system:
        let
          pkgs = pkgsFor system;
          treefmt = treefmtFor pkgs;

          shellFor =
            extra:
            pkgs.mkShell (
              {
                packages = [
                  pkgs.python312
                  pkgs.uv
                  pkgs.godot_4_7
                  pkgs.ffmpeg
                  pkgs.ruff
                  pkgs.rustc
                  pkgs.cargo
                  pkgs.rustfmt
                  pkgs.clippy
                  pkgs.protobuf
                  pkgs.git
                  treefmt
                ]
                ++ lib.optional pkgs.stdenv.hostPlatform.isLinux pkgs.xvfb-run;
                shellHook = ''
                  echo "Python deps come from uv: run 'uv run --frozen chongming-center-bus ...' from the repository root." >&2
                '';
              }
              // runtimeEnv pkgs
              // extra
            );
        in
        {
          default = shellFor { };
          ci = pkgs.mkShell (
            {
              packages = [
                pkgs.python312
                pkgs.uv
                pkgs.ffmpeg
                pkgs.ruff
                pkgs.rustc
                pkgs.cargo
                pkgs.rustfmt
                pkgs.clippy
                pkgs.protobuf
                pkgs.git
                treefmt
              ];
            }
            // pythonEnv pkgs
            // {
              CHONGMING_FFMPEG = lib.getExe pkgs.ffmpeg;
            }
          );
        }
        // lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
          software = shellFor {
            LD_LIBRARY_PATH = lib.makeLibraryPath [
              pkgs.mesa
              pkgs.stdenv.cc.cc.lib
              pkgs.zlib
            ];
            LIBGL_ALWAYS_SOFTWARE = "1";
            LIBGL_DRIVERS_PATH = "${pkgs.mesa}/lib/dri";
            __EGL_VENDOR_LIBRARY_FILENAMES = "${pkgs.mesa}/share/glvnd/egl_vendor.d/50_mesa.json";
            __GLX_VENDOR_LIBRARY_NAME = "mesa";
          };
        };
    in
    {
      apps = lib.genAttrs systems appsFor;
      packages = lib.genAttrs systems localAssetsFor;
      devShells = lib.genAttrs systems devShellsFor;
      formatter = lib.genAttrs systems (system: treefmtFor (pkgsFor system));
    };
}
