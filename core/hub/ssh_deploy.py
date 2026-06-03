"""SSH bootstrap for remote LUCID agent nodes."""
from __future__ import annotations

import gzip
import hashlib
import io
import http.client
import os
import platform as py_platform
import re
import secrets
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import paramiko

from core.common.text_encoding import BYTE_ERROR_ESCAPE, decode_utf8
from core.hub.nodes import HubConfig, NodeConfig, STATE_DIR, agent_get, ensure_tunnels, record_ssh_history, tunnel_error, write_node_config


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KEY = STATE_DIR / "id_ed25519"
DEPLOY_CACHE = Path(os.environ.get("LUCID_DEPLOY_CACHE", str(REPO_ROOT / "deployment_package"))).expanduser()
MICROMAMBA_URL = "https://micro.mamba.pm/api/micromamba/{platform}/latest"
GITHUB_DEPLOYMENT_PACKAGE_URL = (
    "https://github.com/xianyuzhuruc/LUCID/releases/download/deployment_packages/{asset}"
)
DOWNLOAD_RETRIES = 3
DOWNLOAD_RETRY_DELAY_SECONDS = 2
RUNTIME_PYTHON_VERSION = "3.11"
LINUX_VIRTUAL_PACKAGE_VERSION = "5.15.0"
GLIBC_VIRTUAL_PACKAGE_VERSION = "2.17"
MACOS_VIRTUAL_PACKAGE_VERSION = "13.0"
PACKAGE_ARCHIVE_SUFFIXES = (".conda", ".tar.bz2")
SUPPORTED_AGENT_RUNTIME_PLATFORMS = ("linux-amd64", "linux-aarch64", "osx-arm64")
DEFAULT_AGENT_RUNTIME_PLATFORMS = ("linux-amd64", "linux-aarch64", "osx-arm64")
CONDA_PLATFORM_ALIASES = {
    "linux-amd64": "linux-64",
}
GITHUB_AGENT_RUNTIME_SHA256 = {
    "linux-aarch64": "441640b3dcff86a88173dbb8e6a8a2bf342e29d910efa7fa83f0060c73b477b3",
    "linux-amd64": "37280a4c208afd18f54c652b4e79fbac31c332a20967825c8b1af510688bcf5f",
    "osx-arm64": "67b48e2236e70e32a60c68ffad440c85b53ad4d04ac810a522d1f666b40fa761",
}
GITHUB_MICROMAMBA_ASSETS = {
    "linux-aarch64": "lucid-micromamba-linux-aarch64.gz",
    "linux-amd64": "lucid-micromamba-linux-amd64.gz",
    "osx-arm64": "lucid-micromamba-osx-arm64.gz",
}
GITHUB_MICROMAMBA_SHA256 = {
    "linux-aarch64": "6fb5f26f6c287ad37bd59cb6f665d52ecc6314fbebec62a1ca293c6015a652c6",
    "linux-amd64": "53bce558ff311fed7878baa73b013c70b45a2b38284b5a5048bf51cdd872570c",
    "osx-arm64": "102993e5212967aba341dc6de7c0a4062da7d0ac439ae2624fc90714e0e744dd",
}
RUNTIME_PACKAGES = (
    f"python={RUNTIME_PYTHON_VERSION}",
    "tmux",
    "fastapi-core>=0.115",
    "paramiko>=3.5",
    "uvicorn>=0.32",
    "websockets>=10.4",
    "sse-starlette>=2.1",
)
DeployProgress = Callable[[str, str], None]


@dataclass(frozen=True)
class RemotePython:
    command: str
    shell_command: str
    version: str


@dataclass(frozen=True)
class DeployRequest:
    id: str
    host: str
    user: str
    password: str = ""
    identity_file: str = ""
    ssh_port: int = 22
    agent_port: int = 0
    local_port: int = 0
    remote_dir: str = "~/.lucid/agent"
    node_name: str = ""
    python_command: str = "auto"
    remember_password: bool = True


def deploy_agent(req: DeployRequest, progress: DeployProgress | None = None) -> dict:
    """Deploy this checkout to a remote server and persist the hub node config."""
    _report_progress(progress, "validate", "Validating deploy request")
    if not req.id or not req.host or not req.user:
        raise ValueError("id, host, and user are required")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", req.id):
        raise ValueError("id must contain only letters, numbers, dot, underscore, or dash")
    _record_deploy_ssh_history(req)

    _report_progress(progress, "prepare_local", "Preparing local SSH key and source archive")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.chmod(0o700)
    identity_file = _deploy_identity_file(req)
    token = secrets.token_urlsafe(32)
    local_port = req.local_port or _pick_local_port()
    archive = _build_archive()

    _report_progress(progress, "connect_ssh", f"Connecting to {req.user}@{req.host}:{req.ssh_port}")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(**_ssh_connect_kwargs(req))
    try:
        _report_progress(progress, "detect_remote", "Detecting remote path and Linux architecture")
        remote_dir = _remote_expand(client, req.remote_dir)
        if not remote_dir.startswith("/"):
            raise RuntimeError(f"remote directory did not expand to an absolute path: {remote_dir}")
        remote_platform = _remote_agent_platform(client)
        runtime_bundle = _prepare_agent_runtime_bundle(remote_platform, progress)
        tmp_archive = f"/tmp/lucid-agent-{int(time.time())}.tar.gz"
        tmp_runtime = f"/tmp/lucid-runtime-{remote_platform}-{int(time.time())}.tar.gz"
        _report_progress(progress, "upload", f"Uploading app archive and {remote_platform} runtime bundle over SSH")
        with client.open_sftp() as sftp:
            sftp.putfo(io.BytesIO(archive), tmp_archive)
            with runtime_bundle.open("rb") as f:
                sftp.putfo(f, tmp_runtime)
        _report_progress(progress, "stop_agent", "Stopping previous remote agent")
        _stop_remote_agent(client, req.id, remote_dir)
        _report_progress(progress, "install_app", f"Unpacking app into {remote_dir}")
        _run(client, f"mkdir -p {shlex.quote(remote_dir)}")
        _run(client, f"tar -xzf {shlex.quote(tmp_archive)} -C {shlex.quote(remote_dir)}")
        _run(client, f"rm -f {shlex.quote(tmp_archive)}")
        _report_progress(progress, "install_runtime", "Installing bundled runtime on remote host")
        _install_remote_runtime_bundle(client, remote_dir, tmp_runtime)
        _run(client, f"rm -f {shlex.quote(tmp_runtime)}")
        _report_progress(progress, "start_agent", "Starting remote agent")
        remote_python = _bundled_remote_python(remote_dir)
        agent_port = _select_remote_agent_port(client, remote_python, req.agent_port)
        _install_public_key(client, identity_file.with_suffix(".pub"))
        _write_remote_env(client, remote_dir, req, token, remote_python, agent_port)
        _start_remote_agent(client, remote_dir, req.id)
        _report_progress(progress, "health_check", f"Checking remote agent health on 127.0.0.1:{agent_port}")
        health = _wait_remote_agent_ready(client, remote_python, agent_port, token)
    finally:
        client.close()

    _report_progress(progress, "persist_config", "Saving hub node configuration")
    node = NodeConfig(
        id=req.id,
        kind="ssh",
        name=req.node_name or req.id,
        url=f"http://127.0.0.1:{local_port}",
        host=req.host,
        user=req.user,
        ssh_port=req.ssh_port,
        local_port=local_port,
        agent_host="127.0.0.1",
        agent_port=agent_port,
        auto_tunnel=True,
        auto_deploy=True,
        remote_dir=req.remote_dir,
        identity_file=str(identity_file),
        token=token,
    )
    _report_progress(progress, "verify_tunnel", f"Checking local SSH tunnel on 127.0.0.1:{local_port}")
    ensure_tunnels(HubConfig(nodes=(node,)))
    if error := tunnel_error(node.id):
        raise RuntimeError(error)
    agent_get(node, "/agent/v1/snapshot", timeout_ms=5000)
    _report_progress(progress, "persist_config", "Saving hub node configuration")
    write_node_config(node)
    _report_progress(progress, "complete", "Deployment complete")
    return {
        "ok": True,
        "node_id": req.id,
        "host": req.host,
        "user": req.user,
        "local_port": local_port,
        "agent_port": agent_port,
        "remote_dir": req.remote_dir,
        "identity_file": str(identity_file),
        "python": remote_python.command,
        "python_version": remote_python.version,
        "runtime_platform": remote_platform,
        "runtime_bundle": str(runtime_bundle),
        "health": health[-1000:],
    }


def _record_deploy_ssh_history(req: DeployRequest) -> None:
    record_ssh_history({
        "id": req.id,
        "node_name": req.node_name,
        "host": req.host,
        "user": req.user,
        "password": req.password if req.remember_password else "",
        "forget_password": not req.remember_password,
        "ssh_port": req.ssh_port,
        "agent_port": req.agent_port,
        "local_port": req.local_port,
        "remote_dir": req.remote_dir,
        "python_command": req.python_command,
    })


def _report_progress(progress: DeployProgress | None, step: str, message: str) -> None:
    if progress is None:
        return
    progress(step, message)


def _ssh_connect_kwargs(req: DeployRequest) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "hostname": req.host,
        "port": req.ssh_port,
        "username": req.user,
        "timeout": 20,
    }
    if req.password:
        kwargs.update({
            "password": req.password,
            "look_for_keys": False,
            "allow_agent": False,
        })
        return kwargs
    identity_file = Path(req.identity_file).expanduser() if req.identity_file else DEFAULT_KEY
    if identity_file.exists():
        kwargs["key_filename"] = str(identity_file)
    kwargs.update({
        "look_for_keys": True,
        "allow_agent": True,
    })
    return kwargs


def _deploy_identity_file(req: DeployRequest) -> Path:
    if req.identity_file:
        identity_file = Path(req.identity_file).expanduser()
        if identity_file.exists() and identity_file.with_suffix(".pub").exists():
            return identity_file
    return _ensure_local_key()


def _decode_process_output(data: bytes | None) -> str:
    return decode_utf8(data, errors=BYTE_ERROR_ESCAPE)


def _tail_process_output(data: bytes | None, limit: int = 4000) -> str:
    return _decode_process_output(data)[-limit:]


def _ensure_local_key() -> Path:
    if DEFAULT_KEY.exists() and DEFAULT_KEY.with_suffix(".pub").exists():
        return DEFAULT_KEY
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(DEFAULT_KEY)],
        check=True,
        capture_output=True,
    )
    DEFAULT_KEY.chmod(0o600)
    return DEFAULT_KEY


def _build_archive() -> bytes:
    buf = io.BytesIO()
    excluded = {
        ".git",
        ".venv",
        ".lucid-runtime",
        "__pycache__",
        ".pytest_cache",
        "deployment_package",
    }
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in REPO_ROOT.rglob("*"):
            rel = path.relative_to(REPO_ROOT)
            if any(part in excluded for part in rel.parts):
                continue
            if rel.parts[:2] == ("fixtures", "demo-home"):
                continue
            if path.is_file():
                tar.add(path, arcname=str(rel))
    return buf.getvalue()


def _run(client: paramiko.SSHClient, command: str, check: bool = True, timeout: int = 180) -> str:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    out = decode_utf8(stdout.read())
    err = decode_utf8(stderr.read())
    if check and rc != 0:
        raise RuntimeError(f"remote command failed rc={rc}: {command}\nstdout={out[-2000:]}\nstderr={err[-2000:]}")
    return out + err


def _remote_expand(client: paramiko.SSHClient, path: str) -> str:
    cmd = (
        f"remote_path={shlex.quote(path)}\n"
        'case "$remote_path" in\n'
        '  "~") printf "%s\\n" "$HOME" ;;\n'
        '  "~/"*) printf "%s/%s\\n" "$HOME" "${remote_path#\\~/}" ;;\n'
        '  *) printf "%s\\n" "$remote_path" ;;\n'
        "esac\n"
    )
    return _run(client, cmd).strip()


def _remote_agent_platform(client: paramiko.SSHClient) -> str:
    output = _run(client, "uname -s; uname -m").strip().splitlines()
    if len(output) < 2:
        raise RuntimeError(f"remote platform detection failed: {output}")
    system = output[0].strip().lower()
    machine = output[1].strip().lower()
    if system == "linux" and machine in {"x86_64", "amd64"}:
        return "linux-amd64"
    if system == "linux" and machine in {"aarch64", "arm64"}:
        return "linux-aarch64"
    if system == "darwin" and machine in {"aarch64", "arm64"}:
        return "osx-arm64"
    if system == "darwin" and machine in {"x86_64", "amd64"}:
        raise RuntimeError("unsupported remote agent platform: macOS x86_64/amd64 runtime is not bundled")
    raise RuntimeError(f"unsupported remote agent platform: {output[0]}/{machine}")


def _local_micromamba_platform() -> str:
    system = py_platform.system().lower()
    machine = py_platform.machine().lower()
    if system == "linux" and machine in {"x86_64", "amd64"}:
        return "linux-amd64"
    if system == "linux" and machine in {"aarch64", "arm64"}:
        return "linux-aarch64"
    if system == "darwin" and machine in {"x86_64", "amd64"}:
        return "osx-64"
    if system == "darwin" and machine in {"aarch64", "arm64"}:
        return "osx-arm64"
    if system == "windows" and machine in {"x86_64", "amd64"}:
        return "win-64"
    raise RuntimeError(f"unsupported local micromamba platform: {system}/{machine}")


def _conda_platform(platform: str) -> str:
    return CONDA_PLATFORM_ALIASES.get(platform, platform)


def _agent_runtime_asset_name(remote_platform: str) -> str:
    return f"lucid-agent-runtime-{remote_platform}.tar.gz"


def _micromamba_asset_name(mamba_platform: str) -> str:
    return GITHUB_MICROMAMBA_ASSETS.get(mamba_platform, f"lucid-micromamba-{mamba_platform}.gz")


def _ensure_cached_micromamba(mamba_platform: str, progress: DeployProgress | None = None) -> Path:
    binary_name = "micromamba.exe" if mamba_platform.startswith("win-") else "micromamba"
    target = DEPLOY_CACHE / "micromamba" / mamba_platform / binary_name
    compressed_target = target.parent / _micromamba_asset_name(mamba_platform)
    if target.exists():
        _report_progress(progress, "runtime_micromamba", f"Using cached micromamba for {mamba_platform}")
        return target
    if compressed_target.exists():
        _report_progress(progress, "runtime_micromamba", f"Using compressed cached micromamba for {mamba_platform}")
        _decompress_cached_micromamba(compressed_target, target)
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    if _download_github_micromamba(mamba_platform, compressed_target, progress):
        _decompress_cached_micromamba(compressed_target, target)
        return target

    upstream_platform = _conda_platform(mamba_platform)
    url = MICROMAMBA_URL.format(platform=upstream_platform)
    with tempfile.TemporaryDirectory(prefix="lucid-micromamba-", dir=str(target.parent)) as tmp_dir:
        archive = Path(tmp_dir) / "micromamba.tar.bz2"
        _download_url_to_file(url, archive, f"micromamba {mamba_platform}", progress)
        with tarfile.open(archive, "r:bz2") as tar:
            member = _find_micromamba_member(tar, binary_name)
            source = tar.extractfile(member)
            if source is None:
                raise RuntimeError(f"micromamba archive member is not readable: {member.name}")
            with target.open("wb") as f:
                shutil.copyfileobj(source, f)
    target.chmod(0o755)
    return target


def _decompress_cached_micromamba(compressed_target: Path, target: Path) -> None:
    tmp_target = target.with_suffix(f"{target.suffix}.tmp")
    with gzip.open(compressed_target, "rb") as source, tmp_target.open("wb") as dest:
        shutil.copyfileobj(source, dest)
    tmp_target.chmod(0o755)
    tmp_target.replace(target)


def _compress_cached_micromamba(target: Path) -> None:
    if not target.exists():
        return
    compressed_target = target.parent / _micromamba_asset_name(target.parent.name)
    tmp_target = compressed_target.with_suffix(f"{compressed_target.suffix}.tmp")
    with target.open("rb") as source, gzip.open(tmp_target, "wb", compresslevel=9) as dest:
        shutil.copyfileobj(source, dest)
    tmp_target.replace(compressed_target)
    target.unlink()


def _download_url_to_file(
    url: str,
    target: Path,
    label: str,
    progress: DeployProgress | None = None,
) -> None:
    last_error: BaseException | None = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        _report_progress(
            progress,
            "runtime_download",
            f"Downloading {label} (attempt {attempt}/{DOWNLOAD_RETRIES})",
        )
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                with target.open("wb") as f:
                    shutil.copyfileobj(response, f)
            return
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            TimeoutError,
            ConnectionError,
            OSError,
        ) as exc:
            last_error = exc
            if attempt == DOWNLOAD_RETRIES:
                break
            _report_progress(
                progress,
                "runtime_download_retry",
                (
                    f"Download failed for {label}: {type(exc).__name__}: {exc}. "
                    f"Retrying in {DOWNLOAD_RETRY_DELAY_SECONDS}s"
                ),
            )
            time.sleep(DOWNLOAD_RETRY_DELAY_SECONDS)
    if last_error is None:
        raise RuntimeError(f"failed to download {label}: no download attempt was made. url={url!r}")
    raise RuntimeError(
        f"failed to download {label} after {DOWNLOAD_RETRIES} attempts. "
        f"url={url!r} target={str(target)!r} "
        f"last_error={type(last_error).__name__}: {last_error}"
    ) from last_error


def _download_github_deployment_asset(
    asset_name: str,
    expected_sha256: str,
    target: Path,
    label: str,
    progress: DeployProgress | None = None,
) -> bool:
    url = GITHUB_DEPLOYMENT_PACKAGE_URL.format(asset=asset_name)
    tmp_target = target.with_suffix(f"{target.suffix}.download")
    tmp_target.unlink(missing_ok=True)
    try:
        _download_url_to_file(url, tmp_target, label, progress)
        actual_sha256 = _sha256_file(tmp_target)
        if actual_sha256 != expected_sha256:
            raise RuntimeError(
                f"sha256 mismatch for {label}: expected={expected_sha256} actual={actual_sha256}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_target.replace(target)
        return True
    except Exception as exc:
        tmp_target.unlink(missing_ok=True)
        _report_progress(
            progress,
            "runtime_download_retry",
            (
                f"GitHub download failed for {label}: {type(exc).__name__}: {exc}. "
                "Falling back to original download logic"
            ),
        )
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_github_micromamba(
    mamba_platform: str,
    compressed_target: Path,
    progress: DeployProgress | None = None,
) -> bool:
    asset_name = _micromamba_asset_name(mamba_platform)
    expected_sha256 = GITHUB_MICROMAMBA_SHA256.get(mamba_platform)
    if not expected_sha256:
        return False
    _report_progress(progress, "runtime_micromamba", f"Downloading cached micromamba for {mamba_platform} from GitHub")
    return _download_github_deployment_asset(
        asset_name,
        expected_sha256,
        compressed_target,
        f"GitHub micromamba {mamba_platform}",
        progress,
    )


def _download_github_agent_runtime_bundle(
    remote_platform: str,
    bundle: Path,
    progress: DeployProgress | None = None,
) -> bool:
    asset_name = _agent_runtime_asset_name(remote_platform)
    expected_sha256 = GITHUB_AGENT_RUNTIME_SHA256.get(remote_platform)
    if not expected_sha256:
        return False
    _report_progress(progress, "runtime_download", f"Downloading {remote_platform} runtime bundle from GitHub")
    return _download_github_deployment_asset(
        asset_name,
        expected_sha256,
        bundle,
        f"GitHub runtime bundle {remote_platform}",
        progress,
    )


def _find_micromamba_member(tar: tarfile.TarFile, binary_name: str) -> tarfile.TarInfo:
    for member in tar.getmembers():
        name = member.name.replace("\\", "/").rstrip("/")
        if member.isfile() and name.endswith(f"/{binary_name}"):
            return member
    raise RuntimeError(f"micromamba archive did not contain {binary_name}")


def prepare_agent_runtime_bundles(
    platforms: tuple[str, ...] = DEFAULT_AGENT_RUNTIME_PLATFORMS,
    progress: DeployProgress | None = None,
) -> dict[str, Path]:
    bundles: dict[str, Path] = {}
    for platform in platforms:
        _validate_agent_runtime_platform(platform)
        _report_progress(progress, "runtime_platform", f"Preparing runtime bundle for {platform}")
        bundles[platform] = _prepare_agent_runtime_bundle(platform, progress)
    return bundles


def _validate_agent_runtime_platform(platform: str) -> None:
    if platform not in SUPPORTED_AGENT_RUNTIME_PLATFORMS:
        supported = ", ".join(SUPPORTED_AGENT_RUNTIME_PLATFORMS)
        raise ValueError(f"unsupported agent runtime platform: {platform}. supported={supported}")


def _prepare_agent_runtime_bundle(remote_platform: str, progress: DeployProgress | None = None) -> Path:
    _validate_agent_runtime_platform(remote_platform)
    cache_dir = DEPLOY_CACHE / "agent-runtime" / remote_platform
    bundle = cache_dir / _agent_runtime_asset_name(remote_platform)
    if bundle.exists():
        _report_progress(progress, "runtime_cache", f"Using cached runtime bundle for {remote_platform}")
        return bundle

    cache_dir.mkdir(parents=True, exist_ok=True)
    if _download_github_agent_runtime_bundle(remote_platform, bundle, progress):
        return bundle

    _report_progress(progress, "runtime_micromamba", "Preparing local and remote micromamba binaries")
    host_micromamba = _ensure_cached_micromamba(_local_micromamba_platform(), progress)
    remote_micromamba = _ensure_cached_micromamba(remote_platform, progress)
    root_prefix = cache_dir / "m"
    download_prefix = cache_dir / "d"
    shutil.rmtree(download_prefix, ignore_errors=True)
    root_prefix.mkdir(parents=True, exist_ok=True)
    command = [
        str(host_micromamba),
        "create",
        "-y",
        "-p",
        str(download_prefix),
        "--download-only",
        "--platform",
        _conda_platform(remote_platform),
        "--override-channels",
        "-c",
        "conda-forge",
        *RUNTIME_PACKAGES,
    ]
    env = _micromamba_download_env(root_prefix, remote_platform)
    _report_progress(progress, "runtime_download", f"Downloading {remote_platform} runtime packages")
    result = subprocess.run(command, capture_output=True, env=env, timeout=1200)
    if result.returncode != 0:
        raise RuntimeError(
            "failed to download remote runtime packages with micromamba. "
            f"command={command!r}\nstdout={_tail_process_output(result.stdout)}\nstderr={_tail_process_output(result.stderr)}"
        )

    pkgs_dir = root_prefix / "pkgs"
    if not pkgs_dir.exists():
        raise RuntimeError(f"micromamba did not create a package cache at {pkgs_dir}")

    _report_progress(progress, "runtime_pack", "Packing downloaded runtime packages")
    tmp_bundle = bundle.with_suffix(".tmp")
    if tmp_bundle.exists():
        tmp_bundle.unlink()
    with tarfile.open(tmp_bundle, "w:gz") as tar:
        tar.add(remote_micromamba, arcname="bin/micromamba")
        package_count = _add_package_cache_to_tar(tar, pkgs_dir)
        package_text = "\n".join(RUNTIME_PACKAGES) + "\n"
        info = tarfile.TarInfo("runtime-packages.txt")
        data = package_text.encode("utf-8")
        info.size = len(data)
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(data))
    if package_count == 0:
        tmp_bundle.unlink(missing_ok=True)
        raise RuntimeError(
            "micromamba package cache did not contain package archives. "
            f"cache_dir={pkgs_dir}"
        )
    tmp_bundle.replace(bundle)
    _compress_cached_micromamba(host_micromamba)
    _compress_cached_micromamba(remote_micromamba)
    shutil.rmtree(root_prefix, ignore_errors=True)
    shutil.rmtree(download_prefix, ignore_errors=True)
    return bundle


def _add_package_cache_to_tar(tar: tarfile.TarFile, pkgs_dir: Path) -> int:
    tar.add(pkgs_dir, arcname="pkgs", recursive=False)
    package_count = 0
    for path in _iter_package_cache_files(pkgs_dir):
        if path.name.endswith(PACKAGE_ARCHIVE_SUFFIXES):
            package_count += 1
        rel = path.relative_to(pkgs_dir).as_posix()
        tar.add(path, arcname=f"pkgs/{rel}", recursive=False)
    return package_count


def _iter_package_cache_files(pkgs_dir: Path) -> Iterator[Path]:
    for root, dirs, files in os.walk(pkgs_dir):
        root_path = Path(root)
        rel_parts = root_path.relative_to(pkgs_dir).parts
        if rel_parts and rel_parts[0] == "cache":
            dirs[:] = []
            continue
        if _is_extracted_conda_package_dir(root_path):
            dirs[:] = []
            continue
        dirs[:] = [name for name in dirs if name != "__pycache__"]
        for file_name in files:
            if file_name.endswith((".pyc", ".pyo")):
                continue
            yield root_path / file_name


def _is_extracted_conda_package_dir(path: Path) -> bool:
    info_dir = path / "info"
    return (info_dir / "index.json").is_file() or (info_dir / "repodata_record.json").is_file()


def _micromamba_download_env(root_prefix: Path, remote_platform: str) -> dict[str, str]:
    env = os.environ.copy()
    env["MAMBA_ROOT_PREFIX"] = str(root_prefix)
    if remote_platform.startswith("linux-"):
        env["CONDA_OVERRIDE_LINUX"] = LINUX_VIRTUAL_PACKAGE_VERSION
        env["CONDA_OVERRIDE_GLIBC"] = GLIBC_VIRTUAL_PACKAGE_VERSION
    if remote_platform.startswith("osx-"):
        env["CONDA_OVERRIDE_OSX"] = MACOS_VIRTUAL_PACKAGE_VERSION
    return env


def _install_remote_runtime_bundle(client: paramiko.SSHClient, remote_dir: str, remote_bundle: str) -> None:
    runtime_dir = f"{remote_dir}/.lucid-runtime"
    cmd = (
        "set -eu\n"
        f"runtime_dir={shlex.quote(runtime_dir)}\n"
        f"remote_bundle={shlex.quote(remote_bundle)}\n"
        'if [ "${runtime_dir#/}" = "$runtime_dir" ]; then\n'
        '  echo "remote runtime directory is not absolute: $runtime_dir" >&2\n'
        "  exit 1\n"
        "fi\n"
        'env_dir="$runtime_dir/env"\n'
        'bundle_dir="$runtime_dir/bundle"\n'
        'mamba_root="$runtime_dir/mamba-root"\n'
        'mkdir -p "$runtime_dir" "$bundle_dir" "$mamba_root" "$mamba_root/cache" "$mamba_root/home"\n'
        'rm -rf "$bundle_dir"\n'
        'mkdir -p "$bundle_dir"\n'
        'tar -xzf "$remote_bundle" -C "$bundle_dir"\n'
        'mkdir -p "$runtime_dir/bin"\n'
        'cp "$bundle_dir/bin/micromamba" "$runtime_dir/bin/micromamba"\n'
        'chmod 755 "$runtime_dir/bin/micromamba"\n'
        'rm -rf "$mamba_root/pkgs"\n'
        'mv "$bundle_dir/pkgs" "$mamba_root/pkgs"\n'
        'marker="$env_dir/.lucid-runtime.txt"\n'
        'if [ ! -x "$env_dir/bin/python" ] || [ ! -x "$env_dir/bin/tmux" ] || ! cmp -s "$bundle_dir/runtime-packages.txt" "$marker"; then\n'
        '  rm -rf "$env_dir"\n'
        '  explicit="$bundle_dir/runtime-explicit.txt"\n'
        "  {\n"
        "    printf '@EXPLICIT\\n'\n"
        '    find "$mamba_root/pkgs/https" -type f -name urls.txt -exec cat {} \\; 2>/dev/null | sort -u\n'
        '  } > "$explicit"\n'
        '  explicit_lines="$(wc -l < "$explicit" | tr -d " ")"\n'
        '  if [ "$explicit_lines" -le 1 ]; then\n'
        "    {\n"
        "      printf '@EXPLICIT\\n'\n"
        '      find "$mamba_root/pkgs" -type f \\( -name \'*.conda\' -o -name \'*.tar.bz2\' \\) | sort | sed \'s#^#file://#\'\n'
        '    } > "$explicit"\n'
        '    explicit_lines="$(wc -l < "$explicit" | tr -d " ")"\n'
        "  fi\n"
        '  if [ "$explicit_lines" -le 1 ]; then\n'
        '    echo "runtime package cache did not contain package archives" >&2\n'
        "    exit 1\n"
        "  fi\n"
        '  HOME="$mamba_root/home" MAMBA_ROOT_PREFIX="$mamba_root" CONDA_PKGS_DIRS="$mamba_root/pkgs" XDG_CACHE_HOME="$mamba_root/cache" "$runtime_dir/bin/micromamba" create -y -p "$env_dir" --offline --file "$bundle_dir/runtime-explicit.txt"\n'
        '  cp "$bundle_dir/runtime-packages.txt" "$marker"\n'
        "fi\n"
        '"$env_dir/bin/python" - <<\'PY\'\n'
        "import fastapi\n"
        "import paramiko\n"
        "import sse_starlette\n"
        "import uvicorn\n"
        "import websockets\n"
        "PY\n"
        '"$env_dir/bin/tmux" -V >/dev/null\n'
        'rm -rf "$bundle_dir"\n'
    )
    _run(client, cmd, timeout=1200)


def _bundled_remote_python(remote_dir: str) -> RemotePython:
    command = f"{remote_dir}/.lucid-runtime/env/bin/python"
    return RemotePython(command=command, shell_command=shlex.quote(command), version=RUNTIME_PYTHON_VERSION)


def _wait_remote_agent_ready(
    client: paramiko.SSHClient,
    remote_python: RemotePython,
    agent_port: int,
    token: str,
) -> str:
    cmd = (
        f"{remote_python.shell_command} - {agent_port} {shlex.quote(token)} <<'PY'\n"
        "import sys\n"
        "import time\n"
        "import urllib.error\n"
        "import urllib.request\n"
        "\n"
        "port = int(sys.argv[1])\n"
        "token = sys.argv[2]\n"
        "url = f'http://127.0.0.1:{port}/agent/v1/snapshot'\n"
        "opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))\n"
        "last_error = ''\n"
        "for _ in range(40):\n"
        "    req = urllib.request.Request(\n"
        "        url,\n"
        "        headers={'Accept': 'application/json', 'Authorization': f'Bearer {token}'},\n"
        "    )\n"
        "    try:\n"
        "        with opener.open(req, timeout=1) as response:\n"
        "            body = response.read().decode('utf-8', errors='replace')\n"
        "        print(body[:1000])\n"
        "        raise SystemExit(0)\n"
        "    except Exception as exc:\n"
        "        last_error = f'{type(exc).__name__}: {exc}'\n"
        "        time.sleep(0.5)\n"
        "print(f'remote agent did not become ready on 127.0.0.1:{port}: {last_error}', file=sys.stderr)\n"
        "raise SystemExit(1)\n"
        "PY"
    )
    return _run(client, cmd, timeout=60)


def _stop_remote_agent(client: paramiko.SSHClient, node_id: str, remote_dir: str = "") -> None:
    safe_node_id = re.sub(r"[^A-Za-z0-9_.-]", "_", node_id)
    cmd = (
        "set +e\n"
        'state_dir="$HOME/.lucid"\n'
        f'pidfile="$state_dir/agent-{safe_node_id}.pid"\n'
        f"remote_dir={shlex.quote(remote_dir)}\n"
        'if [ -f "$pidfile" ]; then\n'
        '  pid="$(cat "$pidfile" 2>/dev/null || true)"\n'
        '  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then\n'
        '    kill "$pid" || true\n'
        "  fi\n"
        "fi\n"
        'if [ -n "$remote_dir" ] && [ -d /proc ]; then\n'
        '  for pid in $(pgrep -u "$(id -u)" -f "uvicorn app:app --host 127.0.0.1 --port" 2>/dev/null || true); do\n'
        '    cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"\n'
        '    if [ "$cwd" = "$remote_dir" ] || [ "$cwd" = "$remote_dir (deleted)" ]; then\n'
        '      kill "$pid" || true\n'
        "    fi\n"
        "  done\n"
        "fi\n"
        "sleep 1\n"
        'if [ -f "$pidfile" ]; then\n'
        '  pid="$(cat "$pidfile" 2>/dev/null || true)"\n'
        '  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then\n'
        '    kill -9 "$pid" || true\n'
        "  fi\n"
        "fi\n"
        'rm -f "$pidfile"\n'
    )
    _run(client, cmd, check=False)


def _select_remote_agent_port(client: paramiko.SSHClient, remote_python: RemotePython, requested_port: int) -> int:
    requested = max(0, int(requested_port or 0))
    cmd = (
        f"{remote_python.shell_command} - {requested} <<'PY'\n"
        "import socket\n"
        "import sys\n"
        "requested = int(sys.argv[1])\n"
        "\n"
        "def is_free(port: int) -> bool:\n"
        "    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:\n"
        "        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "        try:\n"
        "            sock.bind(('127.0.0.1', port))\n"
        "        except OSError:\n"
        "            return False\n"
        "        return True\n"
        "\n"
        "if requested > 0 and is_free(requested):\n"
        "    print(requested)\n"
        "    raise SystemExit(0)\n"
        "start = requested if requested > 0 else 7879\n"
        "for port in range(start, start + 500):\n"
        "    if 0 < port < 65536 and is_free(port):\n"
        "        print(port)\n"
        "        raise SystemExit(0)\n"
        "with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:\n"
        "    sock.bind(('127.0.0.1', 0))\n"
        "    print(sock.getsockname()[1])\n"
        "PY"
    )
    output = _run(client, cmd).strip()
    try:
        return int(output.splitlines()[-1])
    except (IndexError, ValueError) as e:
        raise RuntimeError(f"remote port selection did not return a valid port: {output!r}") from e


def _install_public_key(client: paramiko.SSHClient, pubkey_path: Path) -> None:
    pub = pubkey_path.read_text(encoding="utf-8").strip()
    quoted = shlex.quote(pub)
    cmd = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
        f"grep -qxF {quoted} ~/.ssh/authorized_keys || echo {quoted} >> ~/.ssh/authorized_keys"
    )
    _run(client, cmd)


def _write_remote_env(
    client: paramiko.SSHClient,
    remote_dir: str,
    req: DeployRequest,
    token: str,
    remote_python: RemotePython,
    agent_port: int,
) -> None:
    env = "\n".join([
        "LUCID_MODE=agent",
        f"LUCID_NODE_ID={shlex.quote(req.id)}",
        "LUCID_AGENT_HOST=127.0.0.1",
        f"LUCID_PORT={agent_port}",
        f"LUCID_AGENT_TOKEN={shlex.quote(token)}",
        f"LUCID_PYTHON={shlex.quote(remote_python.command)}",
        "LUCID_NO_VENV=1",
        "LUCID_SKIP_SYSTEM_DEPS=1",
        "NO_PROXY=127.0.0.1,localhost",
        "no_proxy=127.0.0.1,localhost",
        'LUCID_RUNTIME_DIR="$PWD/.lucid-runtime"',
        'PATH="$PWD/.lucid-runtime/env/bin:$PATH"',
        "",
    ])
    with client.open_sftp() as sftp:
        with sftp.file(f"{remote_dir}/.agent.env", "w") as f:
            f.write(env)


def _start_remote_agent(client: paramiko.SSHClient, remote_dir: str, node_id: str) -> None:
    safe_node_id = re.sub(r"[^A-Za-z0-9_.-]", "_", node_id)
    cmd = (
        "set -eu\n"
        'state_dir="$HOME/.lucid"\n'
        'log_dir="$state_dir/logs"\n'
        'mkdir -p "$log_dir"\n'
        f'pidfile="$state_dir/agent-{safe_node_id}.pid"\n'
        f'log="$log_dir/agent-{safe_node_id}.log"\n'
        f"remote_dir={shlex.quote(remote_dir)}\n"
        'if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then\n'
        '  kill "$(cat "$pidfile")" || true\n'
        "  sleep 1\n"
        "fi\n"
        "agent_cmd='cd \"$1\" && set -a && . ./.agent.env && set +a && exec bash run.sh'\n"
        "if command -v setsid >/dev/null 2>&1; then\n"
        '  setsid bash -lc "$agent_cmd" lucid-agent "$remote_dir" </dev/null >>"$log" 2>&1 &\n'
        "else\n"
        '  nohup bash -lc "$agent_cmd" lucid-agent "$remote_dir" </dev/null >>"$log" 2>&1 &\n'
        "fi\n"
        'pid="$!"\n'
        'printf "%s\\n" "$pid" > "$pidfile"\n'
        'disown "$pid" 2>/dev/null || true\n'
        'printf "%s\\n" "$pid"\n'
    )
    stdin, stdout, stderr = client.exec_command(cmd, timeout=15)
    deadline = time.time() + 5
    while not stdout.channel.exit_status_ready():
        if time.time() >= deadline:
            stdout.channel.close()
            return
        time.sleep(0.1)
    rc = stdout.channel.recv_exit_status()
    out = decode_utf8(stdout.read())
    err = decode_utf8(stderr.read())
    if rc != 0:
        raise RuntimeError(f"failed to start remote agent rc={rc}: stdout={out[-1000:]} stderr={err[-1000:]}")


def _pick_local_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
