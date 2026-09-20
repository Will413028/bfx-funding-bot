"""Verify a single Docker-save OCI artifact without extracting archive paths."""
from __future__ import annotations

import hashlib
import re
import subprocess
import tarfile
from pathlib import Path
from typing import Any, Protocol

from bfx_funding_bot.core.release_identity import PackagedImageIdentity, identity_json

OCI = "application/vnd.oci.image."
MAX_METADATA = 8 * 1024 * 1024
MAX_MEMBERS = 4096
SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")


class PackagingBlocked(RuntimeError):  # noqa: N818
    pass


class ImageNotFound(PackagingBlocked):
    pass


class Runner(Protocol):
    def __call__(self, args: list[str], *, data: bytes | None = None) -> bytes: ...


def run(args: list[str], *, data: bytes | None = None) -> bytes:
    result = subprocess.run(args, input=data, capture_output=True)
    if result.returncode:
        # Only this exact, immutable-image absence permits another lookup.
        if (args[:3] == ["docker", "image", "inspect"] and len(args) == 4
            and SHA.fullmatch(args[3]) and result.stdout.strip() == b"[]"
            and result.stderr.strip() == f"Error response from daemon: No such image: {args[3]}".encode()):
            raise ImageNotFound("image_not_found")
        raise PackagingBlocked("command_failed:" + args[0])
    return result.stdout


def _blob_path(descriptor: dict[str, Any], role: str) -> str:
    if (descriptor["mediaType"] != OCI + role
        or not SHA.fullmatch(descriptor["digest"])
        or type(descriptor["size"]) is not int or descriptor["size"] < 0
        or descriptor.get("urls") or descriptor.get("data")):
        raise ValueError("descriptor")
    return "blobs/sha256/" + descriptor["digest"][7:]


def _bounded_headers(path: Path) -> None:
    """Reject extensions before tarfile can allocate attacker-sized PAX data."""
    with path.open("rb") as stream:
        count = 0
        while True:
            header = stream.read(512)
            if header == bytes(512):
                if stream.read(512) != bytes(512):
                    raise ValueError("tar_terminator")
                while chunk := stream.read(1024 * 1024):
                    if chunk.strip(b"\0"):
                        raise ValueError("trailing_archive")
                return
            if len(header) != 512 or count >= MAX_MEMBERS:
                raise ValueError("tar_header_limit")
            info = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
            if (info.type not in {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE}
                or info.size < 0 or (info.isdir() and info.size)):
                raise ValueError("unsupported_tar_extension")
            count += 1
            stream.seek(((info.size + 511) // 512) * 512, 1)


def inspect_archive(path: Path) -> PackagedImageIdentity:
    """Accept one OCI manifest plus matching Docker metadata and raw tar layers.

    Metadata reads are bounded; layer hashing uses constant memory. Compressed
    layer formats, multi-platform indexes and external descriptors fail closed.
    Docker save's uncompressed layers must also match the config rootfs diff IDs.
    """
    try:
        _bounded_headers(path)
        with tarfile.open(path, "r:") as archive:
            members = {}
            hashes = {}
            for member in archive:
                if len(members) >= MAX_MEMBERS or member.name in members:
                    raise ValueError("duplicate_or_excess_members")
                name = member.name
                if (member.isdir() and name in {"blobs", "blobs/sha256"}):
                    members[name] = member
                    continue
                if (not member.isfile() or not re.fullmatch(
                    r"(?:index\.json|manifest\.json|oci-layout|blobs/sha256/[0-9a-f]{64})", name)):
                    raise ValueError("unsafe_or_unknown_member")
                members[name] = member
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("missing_stream")
                with stream:
                    hashes[name] = hashlib.file_digest(stream, "sha256").hexdigest()

            def read(name: str) -> Any:
                member = members[name]
                if member.size > MAX_METADATA:
                    raise ValueError("metadata_too_large")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("missing_metadata")
                with stream:
                    return identity_json(stream.read(MAX_METADATA + 1))

            def descriptor(desc: dict[str, Any], role: str) -> str:
                name = _blob_path(desc, role)
                if members[name].size != desc["size"] or hashes[name] != desc["digest"][7:]:
                    raise ValueError("descriptor_content")
                return name

            if read("oci-layout") != {"imageLayoutVersion": "1.0.0"}:
                raise ValueError("layout")
            index, docker = read("index.json"), read("manifest.json")
            if (index["schemaVersion"] != 2 or index.get("mediaType") != OCI + "index.v1+json"
                or len(index["manifests"]) != 1 or not isinstance(docker, list) or len(docker) != 1):
                raise ValueError("single_image_required")
            desc = index["manifests"][0]
            manifest_name = descriptor(desc, "manifest.v1+json")
            manifest = read(manifest_name)
            if manifest["schemaVersion"] != 2 or manifest["mediaType"] != OCI + "manifest.v1+json":
                raise ValueError("manifest")
            config_name = descriptor(manifest["config"], "config.v1+json")
            config = read(config_name)
            identity = PackagedImageIdentity(config_digest=manifest["config"]["digest"],
                manifest_digest=desc["digest"], platform=f'{config["os"]}/{config["architecture"]}')
            if "platform" in desc:
                plat = desc["platform"]
                if f'{plat["os"]}/{plat["architecture"]}' != identity.platform:
                    raise ValueError("descriptor_platform")
            layers = [descriptor(layer, "layer.v1.tar") for layer in manifest["layers"]]
            if (config["rootfs"]["type"] != "layers"
                or config["rootfs"]["diff_ids"] != [layer["digest"] for layer in manifest["layers"]]
                or docker[0]["Config"] != config_name or docker[0]["Layers"] != layers):
                raise ValueError("image_linkage")
            if "LayerSources" in docker[0] and docker[0]["LayerSources"] != {
                layer["digest"]: layer for layer in manifest["layers"]
            }:
                raise ValueError("layer_sources")
            extras = set(hashes) - {"index.json", "manifest.json", "oci-layout",
                                    manifest_name, config_name, *layers}
            if extras:
                # Classic Docker save includes v1 compatibility JSON per layer.
                # Neither supported loader selects images from these blobs.
                # Still require content hashes, a single chain, and the exact
                # canonical top config; never accept a second OCI config/index.
                legacy = {}
                # Some Docker builds also write the build container id and the CPU
                # variant into the v1 blob; the release that established this
                # allowlist did not. Neither selects an image, so both are
                # tolerated -- but a variant is a platform claim, so when one is
                # present it must agree with the canonical config rather than
                # merely be ignored.
                allowed = {"id", "parent", "created", "container_config", "config",
                           "architecture", "os", "container", "variant"}
                for name in extras:
                    value = read(name)
                    if (hashes[name] != name.removeprefix("blobs/sha256/")
                        or not isinstance(value, dict) or set(value) - allowed
                        or not re.fullmatch(r"[0-9a-f]{64}", value["id"])
                        or value["id"] in legacy or value["os"] != config["os"]
                        or ("variant" in value and value["variant"] != config.get("variant"))
                        or not isinstance(value["container_config"], dict)):
                        raise ValueError("legacy_metadata")
                    legacy[value["id"]] = value
                if len(legacy) != len(layers):
                    raise ValueError("legacy_layer_count")
                tops = [value for value in legacy.values() if "config" in value]
                # v1 serializes these zero values; OCI config omits them.
                defaults = {"Hostname": "", "Domainname": "", "Image": "",
                    "AttachStdin": False, "AttachStdout": False, "AttachStderr": False,
                    "Tty": False, "OpenStdin": False, "StdinOnce": False,
                    "Volumes": None, "Entrypoint": None, "Labels": None}
                if (len(tops) != 1 or {**defaults, **tops[0]["config"]} != {**defaults, **config["config"]}
                    or tops[0]["architecture"] != config["architecture"]):
                    raise ValueError("legacy_top_config")
                seen = set()
                current = tops[0]
                while True:
                    if current["id"] in seen:
                        raise ValueError("legacy_cycle")
                    seen.add(current["id"])
                    if not current.get("parent"):
                        break
                    current = legacy[current["parent"]]
                if seen != set(legacy):
                    raise ValueError("legacy_disconnected")
            return identity
    except (OSError, ValueError, KeyError, TypeError, IndexError, tarfile.TarError):
        raise PackagingBlocked("invalid_image_archive") from None


def resolve_image(identity: PackagedImageIdentity, runner: Runner) -> str:
    """Resolve only cryptographically bound immutable IDs, never tags."""
    for candidate in (identity.config_digest, identity.manifest_digest):
        try:
            records = identity_json(runner(["docker", "image", "inspect", candidate]))
        except ImageNotFound:
            continue
        except ValueError:
            raise PackagingBlocked("invalid_image_inspection") from None
        try:
            if not isinstance(records, list) or len(records) != 1:
                raise ValueError
            record = records[0]
            actual = record["Id"]
            if actual != candidate or f'{record["Os"]}/{record["Architecture"]}' != identity.platform:
                raise ValueError
            descriptor = record.get("Descriptor")
            if actual == identity.manifest_digest:
                if (not descriptor or descriptor["digest"] != identity.manifest_digest
                    or descriptor["mediaType"] != OCI + "manifest.v1+json"):
                    raise ValueError
            elif descriptor is not None and (
                descriptor["digest"] != identity.manifest_digest
                or descriptor["mediaType"] != OCI + "manifest.v1+json"
            ):
                raise ValueError
            return str(actual)
        except (ValueError, KeyError, TypeError, IndexError):
            raise PackagingBlocked("image_or_platform_mismatch") from None
    raise PackagingBlocked("packaged_image_not_loaded")
