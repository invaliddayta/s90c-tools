{
  description = "S90C tools: root commands and app cleanup";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "aarch64-linux" "x86_64-linux" ];
      eachSystem = nixpkgs.lib.genAttrs systems;
      packageFor = system:
        let
          pkgs = import nixpkgs { inherit system; };
          runtime = with pkgs; [ python3 bash coreutils openssl ];
        in pkgs.stdenvNoCC.mkDerivation {
          pname = "s90c-tools";
          version = "0.2.0";
          src = pkgs.lib.cleanSourceWith {
            src = self;
            filter = path: type:
              let
                relative = pkgs.lib.removePrefix ("${self}/") (toString path);
                top = builtins.head (pkgs.lib.splitString "/" relative);
              in (toString path == toString self) || (
                builtins.elem top [ "tools" "profiles" "docs" "README.md"
                  "SECURITY.md" "THIRD_PARTY.md" "LICENSE" "flake.nix" "flake.lock" ]
                && relative != "tools/app_sync_guard/build"
                && !(pkgs.lib.hasPrefix "tools/app_sync_guard/build/" relative)
                && relative != "tools/tv_access/config.json"
                && !(pkgs.lib.hasInfix "/__pycache__" relative)
                && !(pkgs.lib.hasSuffix ".pyc" relative)
              );
          };
          nativeBuildInputs = with pkgs; [ makeWrapper mono python3 sqlite openssl ];
          buildPhase = ''
            runHook preBuild
            export HOME=$TMPDIR/home
            mkdir -p "$HOME"
            bash tools/app_sync_guard/build.sh
            for tool in tv_access app_sync_guard; do
              (cd tools/$tool && sha256sum -c SHA256SUMS)
            done
            runHook postBuild
          '';
          doCheck = true;
          checkPhase = ''
            runHook preCheck
            python3 -m unittest discover -s tools/tv_access/tests
            python3 -W error::ResourceWarning -m unittest discover -s tools/app_sync_guard/tests
            python3 -W error::ResourceWarning -m unittest discover -s tools/tests
            runHook postCheck
          '';
          installPhase = ''
            runHook preInstall
            destination=$out/share/s90c-tools
            mkdir -p "$destination" "$out/bin"
            cp -R tools profiles docs README.md SECURITY.md THIRD_PARTY.md LICENSE "$destination/"
            find "$destination/tools" -type d '(' -name __pycache__ -o -name tests ')' -prune -exec rm -rf {} +
            for tool in tv-access tv-key tv-shot; do
              makeWrapper "$destination/tools/tv_access/$tool" "$out/bin/$tool" --prefix PATH : ${pkgs.lib.makeBinPath runtime}
            done
            makeWrapper ${pkgs.python3}/bin/python3 "$out/bin/tv-setup" --add-flags "$destination/tools/tv-setup.py" --prefix PATH : ${pkgs.lib.makeBinPath runtime}
            runHook postInstall
          '';
          meta = {
            description = "Root commands and app cleanup for the tested Samsung S90C build";
            license = [ pkgs.lib.licenses.mit pkgs.lib.licenses.unlicense ];
            platforms = systems;
            mainProgram = "tv-access";
          };
        };
    in {
      packages = eachSystem (system: { default = packageFor system; });
      apps = eachSystem (system:
        let
          app = name: {
            type = "app";
            program = "${self.packages.${system}.default}/bin/${name}";
            meta.description = "${name} for the tested S90C";
          };
        in {
          default = app "tv-access";
          tv-access = app "tv-access";
          tv-setup = app "tv-setup";
          tv-key = app "tv-key";
          tv-shot = app "tv-shot";
        });
      checks = eachSystem (system: { offline = self.packages.${system}.default; });
      devShells = eachSystem (system:
        let pkgs = import nixpkgs { inherit system; };
        in { default = pkgs.mkShell {
          packages = with pkgs; [ python3 bash coreutils openssl mono sqlite ];
        }; });
    };
}
