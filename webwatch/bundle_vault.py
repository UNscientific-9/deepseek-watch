"""生产前端版本仓库（全量资源快照）。

与 `webwatch/frontend_evidence.py` 的分工：
- `frontend_evidence` 负责生成本轮的轻量证据包（清单 + 变化摘要）；
- 本模块负责把每个版本的**完整前端资源**按内容哈希存下来，供后续任意事件
  离线读取旧版正文。旧版资源不再依赖 CDN 是否还保留。

实现复用 `webwatch/bundle_mirror.py`（原 `tools/frontend_bundle_mirror.py`），
避免生产与离线工具出现两套解析逻辑。
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any

from . import bundle_mirror as mirror

def _env_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


# 版本仓库保留上限：先保守留 30 个版本 / 2GB；可用环境变量覆盖。
# 非法值不抛异常：配置写错时退回默认值，不能拖垮整个巡检进程。
VAULT_MAX_SNAPSHOTS = _env_positive_int("WEBWATCH_VAULT_MAX_SNAPSHOTS", 30)
VAULT_MAX_BYTES = _env_positive_int("WEBWATCH_VAULT_MAX_BYTES", 2 * 1024**3)


def vault_root(state_dir: Path | str | None) -> Path:
    if state_dir is None:
        raise ValueError("版本仓库需要 state_dir")
    return Path(state_dir) / "bundle-vault"


class BundleVault:
    """只读视图：按 URL 从已保存的版本仓库中取文件正文。"""

    def __init__(self, root: Path | str, source: str):
        self.root = Path(root)
        self.source = str(source)
        self._index: dict[str, dict] | None = None

    def _load_index(self) -> dict[str, dict]:
        if self._index is None:
            index: dict[str, dict] = {}
            # 只读 manifest，不逐文件重算 sha256：快照写入时已经校验过内容。
            # 真正读取 blob 时再校验一次，兼顾启动速度和完整性。
            for snapshot in mirror.list_snapshots(self.root, self.source):
                manifest = snapshot.get("manifest") if isinstance(snapshot, dict) else None
                files = manifest.get("files") if isinstance(manifest, dict) else None
                for item in files or []:
                    if not isinstance(item, dict) or item.get("status") == "missing":
                        continue
                    url = str(item.get("url") or "")
                    blob = str(item.get("blob") or "")
                    digest = str(item.get("hash") or "")
                    if url and blob and digest:
                        index[url] = {
                            "hash": digest,
                            "blob": blob,
                            "kind": str(item.get("kind") or ""),
                        }
            self._index = index
        return self._index

    def read(self, url: str) -> bytes:
        """返回 URL 对应正文；没有保存或内容校验失败时返回空字节。"""
        item = self._load_index().get(str(url or ""))
        if not item:
            return b""
        body = mirror.read_blob(self.root, str(item.get("blob") or ""))
        if body is None:
            return b""
        digest = str(item.get("hash") or "")
        if digest and hashlib.sha256(body).hexdigest() != digest:
            return b""
        return body

    def latest_manifest(self, *, complete_only: bool = True) -> dict | None:
        snapshots = mirror.list_snapshots(self.root, self.source)
        for snapshot in reversed(snapshots):
            manifest = snapshot.get("manifest") if isinstance(snapshot, dict) else None
            if not isinstance(manifest, dict):
                continue
            # 旧工具生成的 manifest 没有 complete 字段，按完整处理；新快照
            # 明确标记 false 时不能拿来判「同版本已保存」，否则缺失主包/资源的
            # 半成品会让后续巡检永久跳过重试。
            if complete_only and not bool(manifest.get("complete", True)):
                continue
            return manifest
        return None


def _current_signature(snapshot: dict | None) -> tuple[str, str, str]:
    """提取当前巡检快照的版本指纹（commit / main.js / 页面 HTML）。"""
    if not isinstance(snapshot, dict):
        return "", "", ""
    front = snapshot.get("frontend") if isinstance(snapshot.get("frontend"), dict) else {}
    build = front.get("build_meta") if isinstance(front.get("build_meta"), dict) else {}
    commit = str(build.get("commitId") or build.get("commit_id") or snapshot.get("commit_id") or "")
    main_js = str(front.get("main_js_url") or "")
    html = str(snapshot.get("html_sha256") or "")
    return commit, main_js, html


def _manifest_signature(manifest: dict | None) -> tuple[str, str, str]:
    if not isinstance(manifest, dict):
        return "", "", ""
    build = manifest.get("build_meta") if isinstance(manifest.get("build_meta"), dict) else {}
    commit = str(build.get("commitId") or build.get("commit_id") or "")
    main_js = str(manifest.get("main_js_url") or "")
    page = manifest.get("page") if isinstance(manifest.get("page"), dict) else {}
    html = str(page.get("html_sha256") or "")
    return commit, main_js, html


def _same_version(current: dict | None, latest: dict | None) -> bool:
    cur = _current_signature(current)
    old = _manifest_signature(latest)
    # 所有双方都有的指纹都必须一致，不能只看 commit 相同就提前返回：
    # 同 commit 的热修可能换了 main.js 或页面 HTML，也必须保存新快照。
    pairs = [(cur[index], old[index]) for index in (0, 1, 2) if cur[index] and old[index]]
    if not pairs:
        return False
    return all(cur_value == old_value for cur_value, old_value in pairs)


def needs_snapshot(root: Path | str, source: str, current: dict | None) -> bool:
    """当前巡检版本是否还没有**完整**的全量快照。"""
    vault = BundleVault(root, source)
    latest = vault.latest_manifest(complete_only=True)
    if not latest:
        return True
    return not _same_version(current, latest)


def _blob_bytes(root: Path) -> int:
    blobs = root / "blobs"
    if not blobs.is_dir():
        return 0
    total = 0
    for path in blobs.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return total


def _referenced_blobs(root: Path) -> set[Path]:
    """收集所有 source、所有剩余快照引用的 blob；blob 是全局共享的。"""
    referenced: set[Path] = set()
    snap_root = root / "snapshots"
    if not snap_root.is_dir():
        return referenced
    for source_dir in snap_root.iterdir():
        if not source_dir.is_dir():
            continue
        for snapshot in mirror.list_snapshots(root, source_dir.name):
            manifest = snapshot.get("manifest") if isinstance(snapshot, dict) else None
            if not isinstance(manifest, dict):
                continue
            files = manifest.get("files") or []
            page = manifest.get("page") if isinstance(manifest.get("page"), dict) else {}
            blobs = [str(item.get("blob") or "") for item in files if isinstance(item, dict)]
            blobs += [str(page.get("page_html_blob") or ""), str(page.get("page_text_blob") or "")]
            for blob in blobs:
                if blob:
                    try:
                        referenced.add((root / blob).resolve())
                    except OSError:
                        pass
    return referenced


def _delete_unreferenced_blobs(root: Path) -> None:
    """回收所有快照都不再引用的 blob。"""
    referenced = _referenced_blobs(root)
    blobs_root = root / "blobs"
    if not blobs_root.is_dir():
        return
    for path in blobs_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved not in referenced:
            try:
                path.unlink()
            except OSError:
                pass
    for path in sorted(blobs_root.rglob("*"), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass


def _snapshot_sources(root: Path) -> list[str]:
    snap_root = root / "snapshots"
    if not snap_root.is_dir():
        return []
    return [p.name for p in snap_root.iterdir() if p.is_dir()]


def _delete_oldest_snapshot(root: Path, source: str) -> bool:
    snapshots = mirror.list_snapshots(root, source)
    if len(snapshots) <= 1:
        return False
    try:
        shutil.rmtree(snapshots[0]["dir"])
    except OSError:
        return False
    return True


def prune(root: Path | str, source: str | None = None) -> None:
    """删除过旧版本快照，并回收不再被引用的 blob。"""
    root = Path(root)
    snap_root = root / "snapshots"
    if not snap_root.is_dir():
        return
    sources = [source] if source else _snapshot_sources(root)
    # 1) 每个 source 的版本数上限。
    for name in sources:
        snapshots = mirror.list_snapshots(root, name)
        while len(snapshots) > VAULT_MAX_SNAPSHOTS:
            if not _delete_oldest_snapshot(root, name):
                break
            snapshots = mirror.list_snapshots(root, name)
    _delete_unreferenced_blobs(root)

    # 2) 全仓 blob 总字节上限：先回收无引用 blob，再按最旧快照逐个删。
    #    不能只看单 source，否则另一个 source 的占用会把本 source 删到只剩一版。
    while _blob_bytes(root) > VAULT_MAX_BYTES:
        candidates: list[tuple[str, str]] = []
        for name in _snapshot_sources(root):
            snapshots = mirror.list_snapshots(root, name)
            if len(snapshots) > 1:
                candidates.append((snapshots[0]["dir"].name, name))
        if not candidates:
            break
        _, oldest_source = min(candidates)
        if not _delete_oldest_snapshot(root, oldest_source):
            break
        _delete_unreferenced_blobs(root)


def ensure_snapshot(
    target: dict[str, Any],
    state_dir: Path | str,
    *,
    current: dict | None = None,
) -> dict | None:
    """需要时抓取并保存全量前端快照；返回 mirror summary 或 None。"""
    if state_dir is None:
        return None
    source = str(target.get("source") or target.get("label") or "page")
    root = vault_root(state_dir)
    if not needs_snapshot(root, source, current):
        return None
    result = mirror.create_snapshot(target, root)
    summary = result.get("summary") if isinstance(result, dict) else None
    if isinstance(summary, dict) and summary.get("snapshot_id"):
        # 页面抓取失败时 summary 没有 snapshot_id；不要因此误触发 prune。
        prune(root, source)
    return summary if isinstance(summary, dict) else None
