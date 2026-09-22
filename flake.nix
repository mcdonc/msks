{
  description = "msks: workspaces as microvms — the msksd package and its NixOS module";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/21a67dc470149f337cecafbe965d8d252a390518";

  outputs =
    {
      self,
      nixpkgs,
    }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
        config = { };
        overlays = [ ];
      };
      msksd = pkgs.python314.pkgs.callPackage ./nix/msks-pkg.nix {
        textual = pkgs.python314.pkgs.callPackage ./nix/textual-pkg.nix { };
        netfilterqueue =
          pkgs.python314.pkgs.callPackage ./nix/netfilterqueue-pkg.nix
            { };
      };
    in
    {
      packages.${system} = {
        msksd = msksd;
        default = msksd;
      };

      nixosModules.msks = import ./nix/module.nix;
      nixosModules.default = self.nixosModules.msks;
    };
}
