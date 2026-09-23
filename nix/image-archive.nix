# The canonical image artifact packer (#40, shared by every guest
# build since #250): a container-image tar (`podman load`
# compatible) in the containerDisk convention — one layer carrying
# boot/ (kernel, initrd) and disk/ (rootfs.ext4, image.json schema
# 2). Importable with podman/skopeo/plain tar.
#
# The layout is the one `podman save` writes (manifest.json +
# <id>/{layer.tar,json,VERSION} + repositories; the format
# originates with `docker save`, which is the last time docker is
# mentioned here). The layer is UNCOMPRESSED (members readable in
# place with `tar tf`, no decompression at import) and byte-stable
# (--sort=name --mtime=@1 --owner=0 --group=0 --numeric-owner), so
# identical rebuilds hash identically and the per-hash cache
# dedupes across hosts and CI.
{
  lib,
  pkgs,
}:

{
  # bootTree: a directory holding boot/{vmlinuz,initrd.img} and
  # disk/{rootfs.ext4,image.json}. imageName/imageVersion: the
  # catalog identity (<name>:<version>) — they only pick the
  # RepoTag and the archive's file name; the manifest inside the
  # boot tree carries the authoritative image.json.
  mkImageArchive =
    {
      bootTree,
      imageName,
      imageVersion,
    }:
    pkgs.runCommand "msks-image-archive"
      {
        inherit
          bootTree
          imageName
          imageVersion
          ;
        nativeBuildInputs = [ pkgs.gnutar ];
        imageId =
          "msks" + builtins.hashString "sha256" (imageName + ":" + imageVersion);
      }
      ''
        set -eu
        mkdir work
        # The layer: the containerDisk tree, uncompressed, sorted,
        # zeroed timestamps and ownership.
        tar --sort=name --mtime='@1' --owner=0 --group=0 --numeric-owner \
          -C "${bootTree}" -cf work/layer.tar .
        # Container-image bookkeeping.
        mkdir "work/$imageId"
        mv work/layer.tar "work/$imageId/layer.tar"
        printf '1.0' > "work/$imageId/VERSION"
        # A minimally valid image config: podman requires the rootfs
        # diff_ids (the uncompressed layer's digest).
        layer_digest=$(sha256sum "work/$imageId/layer.tar" | cut -d' ' -f1)
        printf '%s' \
          '{"architecture":"amd64","os":"linux","config":{},' \
          '"rootfs":{"type":"layers","diff_ids":["sha256:'"$layer_digest"'"]}}' \
          > "work/$imageId/json"
        # Unquoted heredocs: the env-provided name/version/imageId
        # expand in the shell.
        cat > work/manifest.json <<EOF
        [{"Config":"$imageId/json","RepoTags":["workspace-''${imageName}:''${imageVersion}"],"Layers":["$imageId/layer.tar"]}]
        EOF
        cat > work/repositories <<EOF
        {"workspace-''${imageName}":{"''${imageVersion}":"$imageId"}}
        EOF
        tar --sort=name --mtime='@1' --owner=0 --group=0 --numeric-owner \
          -C work -cf "$out" manifest.json repositories "$imageId"
      '';
}
