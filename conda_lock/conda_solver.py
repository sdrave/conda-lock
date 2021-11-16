import json
import logging
import os
import pathlib
import shlex
import subprocess
import sys
import tarfile
import tempfile

from contextlib import contextmanager
from typing import (
    Any,
    ContextManager,
    Dict,
    Iterable,
    List,
    MutableSequence,
    Optional,
    Sequence,
    TypedDict,
    cast,
)
from urllib.parse import urlsplit, urlunsplit

from conda_lock.invoke_conda import (
    PathLike,
    _get_conda_flags,
    conda_env_override,
    conda_pkgs_dir,
    is_micromamba,
)
from conda_lock.src_parser import (
    Dependency,
    LockedDependency,
    Package,
    VersionedDependency,
    _apply_categories,
)


class FetchAction(TypedDict):
    channel: str
    constrains: Optional[List[str]]
    depends: Optional[List[str]]
    fn: str
    md5: str
    name: str
    subdir: str
    timestamp: int
    url: str
    version: str


class LinkAction(TypedDict):
    base_url: str
    channel: str
    dist_name: str
    name: str
    platform: str
    version: str


class InstallActions(TypedDict):
    LINK: List[LinkAction]
    FETCH: List[FetchAction]


class DryRunInstall(TypedDict):
    actions: InstallActions


def _to_match_spec(conda_dep_name, conda_version):
    if conda_version:
        spec = f"{conda_dep_name}[version='{conda_version}']"
    else:
        spec = f"{conda_dep_name}"
    return spec


def solve_conda(
    conda: PathLike,
    specs: Dict[str, Dependency],
    locked: Dict[str, LockedDependency],
    update: List[str],
    platform: str,
    channels: List[str],
) -> Dict[str, LockedDependency]:

    conda_specs = [
        _to_match_spec(dep.name, dep.version)
        for dep in specs.values()
        if isinstance(dep, VersionedDependency) and dep.manager == "conda"
    ]
    conda_locked = {dep.name: dep for dep in locked.values() if dep.manager == "conda"}
    to_update = set(update).intersection(conda_locked)

    if to_update:
        dry_run_install = update_specs_for_arch(
            conda=conda,
            platform=platform,
            channels=channels,
            specs=conda_specs,
            locked=conda_locked,
            update=list(to_update),
        )
    else:
        dry_run_install = solve_specs_for_arch(
            conda=conda,
            platform=platform,
            channels=channels,
            specs=conda_specs,
        )
    logging.debug("dry_run_install:\n%s", dry_run_install)

    # extract dependencies from package plan
    planned = {
        action["name"]: LockedDependency(
            name=action["name"],
            version=action["version"],
            manager="conda",
            platform=platform,
            dependencies={
                item.split()[0]: " ".join(item.split(" ")[1:])
                for item in action.get("depends") or []
            },
            url=action["url"],
            # NB: virtual packages may have no hash
            hash=action.get("md5", ""),
        )
        for action in dry_run_install["actions"]["FETCH"]
    }

    # propagate categories from explicit to transitive dependencies
    _apply_categories({k: v for k, v in specs.items() if v.manager == "conda"}, planned)

    return planned


def _reconstruct_fetch_actions(
    conda: PathLike, platform: str, dry_run_install: DryRunInstall
) -> DryRunInstall:
    """
    Conda may choose to link a previously downloaded distribution from pkgs_dirs rather
    than downloading a fresh one. Find the repodata record in existing distributions
    that have only a LINK action, and use it to synthesize an equivalent FETCH action.
    """
    link_actions = {p["name"]: p for p in dry_run_install["actions"]["LINK"]}
    fetch_actions = {p["name"]: p for p in dry_run_install["actions"]["LINK"]}
    link_only_names = set(link_actions.keys()).difference(fetch_actions.keys())
    if link_only_names:
        assert not is_micromamba(conda), "micromamba should not cache packages"
        pkgs_dirs = [
            pathlib.Path(d)
            for d in json.loads(
                subprocess.check_output(
                    [str(conda), "info", "--json"], env=conda_env_override(platform)
                )
            )["pkgs_dirs"]
        ]
    else:
        pkgs_dirs = []

    for link_pkg_name in link_only_names:
        link_action = link_actions[link_pkg_name]
        for pkgs_dir in pkgs_dirs:
            record = (
                pkgs_dir / link_action["dist_name"] / "info" / "repodata_record.json"
            )
            if record.exists():
                with open(record) as f:
                    repodata: FetchAction = json.load(f)
                break
        else:
            raise FileExistsError(
                f'Distribution \'{link_action["dist_name"]}\' not found in pkgs_dirs {pkgs_dirs}'
            )
        dry_run_install["actions"]["FETCH"].append(repodata)
    return dry_run_install


def solve_specs_for_arch(
    conda: PathLike,
    channels: Sequence[str],
    specs: List[str],
    platform: str,
) -> DryRunInstall:
    args: MutableSequence[str] = [
        str(conda),
        "create",
        "--prefix",
        os.path.join(conda_pkgs_dir(), "prefix"),
        "--dry-run",
        "--json",
    ]
    conda_flags = os.environ.get("CONDA_FLAGS")
    if conda_flags:
        args.extend(shlex.split(conda_flags))
    if channels:
        args.append("--override-channels")

    for channel in channels:
        args.extend(["--channel", channel])
        if channel == "defaults" and platform in {"win-64", "win-32"}:
            # msys2 is a windows-only channel that conda automatically
            # injects if the host platform is Windows. If our host
            # platform is not Windows, we need to add it manually
            args.extend(["--channel", "msys2"])
    args.extend(specs)
    proc = subprocess.run(
        args,
        env=conda_env_override(platform),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf8",
    )

    def print_proc(proc):
        print(f"    Command: {proc.args}")
        if proc.stdout:
            print(f"    STDOUT:\n{proc.stdout}")
        if proc.stderr:
            print(f"    STDERR:\n{proc.stderr}")

    try:
        proc.check_returncode()
    except subprocess.CalledProcessError:
        try:
            err_json = json.loads(proc.stdout)
            try:
                message = err_json["message"]
            except KeyError:
                print("Message key not found in json! returning the full json text")
                message = err_json
        except json.JSONDecodeError as e:
            print(f"Failed to parse json, {e}")
            message = proc.stdout

        print(f"Could not lock the environment for platform {platform}")
        if message:
            print(message)
        print_proc(proc)

        sys.exit(1)

    try:
        dryrun_install: DryRunInstall = json.loads(proc.stdout)
        return _reconstruct_fetch_actions(conda, platform, dryrun_install)
    except json.JSONDecodeError:
        print("Could not solve for lock")
        print_proc(proc)
        sys.exit(1)


def update_specs_for_arch(
    conda: PathLike,
    specs: List[str],
    locked: Dict[str, LockedDependency],
    update: List[str],
    platform: str,
    channels: Sequence[str],
) -> DryRunInstall:

    with fake_conda_environment(locked.values(), platform=platform) as prefix:
        installed: Dict[str, LinkAction] = {
            entry["name"]: entry
            for entry in json.loads(
                subprocess.check_output(
                    [str(conda), "list", "-p", prefix, "--json"],
                    env=conda_env_override(platform),
                )
            )
        }
        spec_for_name = {v.split("[")[0]: v for v in specs}
        to_update = [
            spec_for_name[name] for name in set(installed).intersection(update)
        ]
        if to_update:
            # NB: micromamba and mainline conda have different semantics for `install` and `update`
            # - conda:
            #   * update -> apply all nonmajor updates unconditionally
            #   * install -> install or update target to latest version compatible with constraint
            # - micromamba:
            #   * update -> update target to latest version compatible with constraint
            #   * install -> update target if current version incompatible with constraint, otherwise _do nothing_
            # Our `update` should always update the target to the latest version compatible with the constraint
            args = [
                str(conda),
                "update" if is_micromamba(conda) else "install",
                *_get_conda_flags(channels=channels, platform=platform),
            ]
            proc = subprocess.run(
                args + ["-p", prefix, "--json", "--dry-run", *to_update],
                env=conda_env_override(platform),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf8",
            )

            try:
                proc.check_returncode()
            except subprocess.CalledProcessError as exc:
                err_json = json.loads(proc.stdout)
                raise RuntimeError(
                    f"Could not lock the environment for platform {platform}: {err_json.get('message')}"
                ) from exc

            dryrun_install: DryRunInstall = json.loads(proc.stdout)
        else:
            dryrun_install = {"actions": {"LINK": [], "FETCH": []}}

        if "actions" not in dryrun_install:
            dryrun_install["actions"] = {"LINK": [], "FETCH": []}

        updated = {entry["name"]: entry for entry in dryrun_install["actions"]["LINK"]}
        for package in set(installed).difference(updated):
            entry = installed[package]
            fn = f'{entry["dist_name"]}.tar.bz2'
            if is_micromamba(conda):
                channel = f'{entry["base_url"]}'
            else:
                channel = f'{entry["base_url"]}/{entry["platform"]}'
            url = f"{channel}/{fn}"
            md5 = locked[package].hash
            dryrun_install["actions"]["FETCH"].append(
                {
                    "name": entry["name"],
                    "channel": channel,
                    "url": url,
                    "fn": fn,
                    "md5": md5,
                    "version": entry["version"],
                    "depends": [
                        f"{k} {v}".strip()
                        for k, v in locked[entry["name"]].dependencies.items()
                    ],
                    "constrains": [],
                    "subdir": entry["platform"],
                    "timestamp": 0,
                }
            )
            dryrun_install["actions"]["LINK"].append(entry)
        return _reconstruct_fetch_actions(conda, platform, dryrun_install)


@contextmanager
def fake_conda_environment(locked: Iterable[LockedDependency], platform: str):
    """
    Create a fake conda prefix containing metadata corresponding to the provided package URLs
    """
    with tempfile.TemporaryDirectory() as prefix:
        conda_meta = pathlib.Path(prefix) / "conda-meta"
        conda_meta.mkdir()
        (conda_meta / "history").touch()
        for dep in (
            dep for dep in locked if dep.manager == "conda" and dep.platform == platform
        ):
            url = urlsplit(dep.url)
            path = pathlib.Path(url.path)
            channel = urlunsplit(
                (url.scheme, url.hostname, str(path.parent), None, None)
            )
            while path.suffix in {".tar", ".bz2", ".gz", ".conda"}:
                path = path.with_suffix("")
            build = path.name.split("-")[-1]
            try:
                build_number = int(build.split("_")[-1])
            except ValueError:
                build_number = 0
            entry = {
                "name": dep.name,
                "channel": channel,
                "url": dep.url,
                "md5": dep.hash,
                "build": build,
                "build_number": build_number,
                "version": dep.version,
                "subdir": path.parent.name,
                "depends": [f"{k} {v}".strip() for k, v in dep.dependencies.items()],
            }
            with open(conda_meta / (path.name + ".json"), "w") as f:
                json.dump(entry, f, indent=2)
        yield prefix
