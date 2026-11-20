"""
MoviePilot V3 插件：H&R 做种清理（HrCleanup）

功能概述
--------
针对「订阅下载 → 媒体整理复制到 115 网盘 Plus」的场景，在满足下列条件时，
自动删除【下载器种子任务 + 下载历史条目 + 硬盘上的原始下载文件】，
115 网盘上的那份【永久保留】：

    G1  已成功整理到目标存储（自动识别含 115 的存储，如 u115 / 115网盘Plus）—— 以「媒体整理记录成功」为准
    G2  复制成功确认（默认只看整理记录 status==True；可选开启云端原路径复核）
    G3  做种时长达标（seeding_time >= max(min_seed_hours, hr_hours + extra_hours)）
    G4  源文件路径位于配置的下载目录内，且不在媒体库目录内

任何一条不满足 → 整个任务跳过，绝不部分删除。默认演练模式（dry_run），
只记录不删除，观察一轮确认无误后再关闭。

所有宿主导入均使用 try/except 双路径（app.sdk.* 优先，回退旧 app.*），
以兼容 MoviePilot V3 的各小版本。
"""

import os
import time
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

# ---------------------------------------------------------------------------
# 插件基类 / 日志（V3 优先，旧版本回退）
# ---------------------------------------------------------------------------
try:
    from app.sdk.plugin import _PluginBase
except Exception:  # noqa: BLE001
    from app.plugins import _PluginBase  # type: ignore

try:
    from app.sdk.logging import logger
except Exception:  # noqa: BLE001
    from app.log import logger  # type: ignore

# ---------------------------------------------------------------------------
# 宿主能力导入（全部软导入，缺失时降级并在运行期报错提示）
# ---------------------------------------------------------------------------
list_download_history = None
list_transfer_history = None
list_subscriptions = None
try:
    from app.sdk.queries import (  # type: ignore
        list_download_history,
        list_transfer_history,
        list_subscriptions,
    )
except Exception:  # noqa: BLE001
    try:
        from app.sdk.queries import list_download_history  # type: ignore
    except Exception:  # noqa: BLE001
        list_download_history = None
    try:
        from app.sdk.queries import list_transfer_history  # type: ignore
    except Exception:  # noqa: BLE001
        list_transfer_history = None
    try:
        from app.sdk.queries import list_subscriptions  # type: ignore
    except Exception:  # noqa: BLE001
        list_subscriptions = None

try:
    from app.chain.storage import StorageChain  # type: ignore
except Exception:  # noqa: BLE001
    StorageChain = None

try:
    from app.chain.download import DownloadChain  # type: ignore
except Exception:  # noqa: BLE001
    DownloadChain = None

try:
    from app.schemas.file import FileItem  # type: ignore
except Exception:  # noqa: BLE001
    FileItem = None

try:
    from app.sdk.services import DownloaderHelper  # type: ignore
except Exception:  # noqa: BLE001
    try:
        from app.application.downloader import DownloaderHelper  # type: ignore
    except Exception:  # noqa: BLE001
        DownloaderHelper = None

try:
    from app.db.oper.downloadhistory import DownloadHistoryOper  # type: ignore
except Exception:  # noqa: BLE001
    try:
        from app.db.downloadhistory_oper import DownloadHistoryOper  # type: ignore
    except Exception:  # noqa: BLE001
        DownloadHistoryOper = None

try:
    from app.schemas.types import StorageSchema  # type: ignore
except Exception:  # noqa: BLE001
    StorageSchema = None

try:
    from app.schemas.types import SystemConfigKey  # type: ignore
except Exception:  # noqa: BLE001
    SystemConfigKey = None

try:
    from app.schemas.types import MessageType  # type: ignore
except Exception:  # noqa: BLE001
    MessageType = None

_STATE_KEY = "hrcleanup_state"
_LOG_LIMIT = 200
_RECORD_LIMIT = 500


def _dt(value: Any) -> Optional[datetime]:
    """把各类日期值统一成 naive datetime。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except Exception:  # noqa: BLE001
        return None


def _is_under(path: str, root: str) -> bool:
    """判断 path 是否位于 root 目录之下（POSIX 语义，适配 NAS）。"""
    if not path or not root:
        return False
    try:
        p = PurePosixPath(str(path).replace("\\", "/")).as_posix().rstrip("/")
        r = PurePosixPath(str(root).replace("\\", "/")).as_posix().rstrip("/")
        return p == r or p.startswith(r + "/")
    except Exception:  # noqa: BLE001
        return False


class HrCleanup(_PluginBase):
    # ------------------------------------------------------------------
    # 元数据
    # ------------------------------------------------------------------
    plugin_name = "H&R 做种清理"
    plugin_desc = "订阅下载经媒体整理复制到 115 网盘后，做种达标且超过 H&R+8 小时，自动删除种子、下载条目与硬盘原始文件（115 副本保留）"
    plugin_icon = "mdi-broom"
    plugin_color = "#22D3EE"
    plugin_version = "1.3.0"
    plugin_author = "RDevils7"
    author_url = "https://github.com/RDevils7"
    plugin_config_prefix = "hrcleanup_"
    plugin_order = 60
    auth_level = 2

    # ------------------------------------------------------------------
    # 配置项默认值（写成类属性，避免首次安装未保存配置时属性缺失）
    # ------------------------------------------------------------------
    _enabled: bool = False
    _dry_run: bool = True
    _cron: str = "0 4 * * *"
    _min_seed_hours: int = 36
    _hr_hours: int = 24
    _extra_hours: int = 8
    # 目标存储标识：留空则自动识别（取 Storages 中类型/名称含 "115" 的项，如 u115 / 115网盘Plus）；
    # 也可手工填成你系统里 115 存储的真实 type，例如 "115网盘Plus"。
    _dest_storage: str = ""
    _only_subscribed: bool = True
    _max_per_run: int = 5
    _scan_days: int = 30
    # 说明：115 会在 H&R 阈值内经「最近接收/需整理文件」把副本转移到正式目录，
    # 到清理时原 dest 路径通常已不存在。因此默认「不按原路径复核云端」，
    # 仅以「媒体整理记录成功」作为复制成功判据。需要时可开启本项。
    _verify_remote: bool = False
    _verify_size: bool = True
    # 删除方式：True = 调用下载器「同时删除已下载的文件」（qb: delete_files=true），
    # 由下载器自己删硬盘文件，删完再校验是否真的没了；残留再由插件兜底删除。
    _delete_with_files: bool = True
    # 种子已不在下载器时的策略：
    #   assume_done = 视为做种义务已结束、允许清理（推荐：任务已消失说明已停止做种，
    #                 否则文件会永远残留；且此时删文件不会加剧 H&R，因为早已停种）
    #   skip        = 无法校验做种时长，保守跳过
    _missing_torrent_policy: str = "assume_done"
    _delete_transfer_history: bool = False
    _extra_allow_paths: str = ""
    _notify: bool = True
    _notify_only_on_action: bool = True
    _retry_cooldown_hours: int = 24

    def __init__(self):
        super().__init__()
        self._state: Dict[str, Any] = {}

    # ==================================================================
    # 生命周期
    # ==================================================================
    def init_plugin(self, config: Optional[dict] = None):
        config = config or {}

        def _get_bool(key: str, default: bool) -> bool:
            val = config.get(key, default)
            if isinstance(val, bool):
                return val
            return str(val).strip().lower() in ("1", "true", "yes", "on")

        def _get_int(key: str, default: int) -> int:
            try:
                return int(config.get(key, default))
            except Exception:  # noqa: BLE001
                return default

        def _get_float(key: str, default: float) -> float:
            try:
                return float(config.get(key, default))
            except Exception:  # noqa: BLE001
                return default

        self._enabled = _get_bool("enabled", self._enabled)
        self._dry_run = _get_bool("dry_run", self._dry_run)
        self._cron = str(config.get("cron", self._cron) or "0 4 * * *").strip()
        self._min_seed_hours = _get_float("min_seed_hours", self._min_seed_hours)
        self._hr_hours = _get_float("hr_hours", self._hr_hours)
        self._extra_hours = _get_float("extra_hours", self._extra_hours)
        self._dest_storage = str(config.get("dest_storage", self._dest_storage) or "").strip()
        self._dest_types_memo: Optional[List[str]] = None
        self._only_subscribed = _get_bool("only_subscribed", self._only_subscribed)
        self._max_per_run = max(1, _get_int("max_per_run", self._max_per_run))
        self._scan_days = max(1, _get_int("scan_days", self._scan_days))
        self._verify_remote = _get_bool("verify_remote", self._verify_remote)
        self._verify_size = _get_bool("verify_size", self._verify_size)
        self._delete_with_files = _get_bool("delete_with_files", self._delete_with_files)
        self._missing_torrent_policy = str(
            config.get("missing_torrent_policy", self._missing_torrent_policy) or "assume_done"
        ).strip().lower()
        if self._missing_torrent_policy not in ("assume_done", "skip"):
            self._missing_torrent_policy = "assume_done"
        self._delete_transfer_history = _get_bool(
            "delete_transfer_history", self._delete_transfer_history
        )
        self._extra_allow_paths = str(config.get("extra_allow_paths", self._extra_allow_paths) or "")
        self._notify = _get_bool("notify", self._notify)
        self._notify_only_on_action = _get_bool(
            "notify_only_on_action", self._notify_only_on_action
        )
        self._retry_cooldown_hours = max(0, _get_int("retry_cooldown_hours", self._retry_cooldown_hours))

        self._state = self._load_state()

    def get_state(self) -> bool:
        return bool(self._enabled)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def stop_service(self):
        pass

    # ==================================================================
    # 配置持久化
    # ==================================================================
    def _save_config(self):
        try:
            self.update_config(
                {
                    "enabled": self._enabled,
                    "dry_run": self._dry_run,
                    "cron": self._cron,
                    "min_seed_hours": self._min_seed_hours,
                    "hr_hours": self._hr_hours,
                    "extra_hours": self._extra_hours,
                    "dest_storage": self._dest_storage,
                    "only_subscribed": self._only_subscribed,
                    "max_per_run": self._max_per_run,
                    "scan_days": self._scan_days,
                    "verify_remote": self._verify_remote,
                    "verify_size": self._verify_size,
                    "delete_with_files": self._delete_with_files,
                    "missing_torrent_policy": self._missing_torrent_policy,
                    "delete_transfer_history": self._delete_transfer_history,
                    "extra_allow_paths": self._extra_allow_paths,
                    "notify": self._notify,
                    "notify_only_on_action": self._notify_only_on_action,
                    "retry_cooldown_hours": self._retry_cooldown_hours,
                }
            )
        except Exception as err:  # noqa: BLE001
            logger.error(f"HrCleanup 保存配置失败：{err}")

    # ==================================================================
    # 状态/审计记录
    # ==================================================================
    def _load_state(self) -> Dict[str, Any]:
        try:
            state = self.get_data(_STATE_KEY) or {}
            if not isinstance(state, dict):
                return {"failures": {}, "log": [], "records": [], "stats": {}}
            state.setdefault("failures", {})
            state.setdefault("log", [])
            state.setdefault("records", [])
            state.setdefault("stats", {})
            return state
        except Exception:  # noqa: BLE001
            return {"failures": {}, "log": [], "records": [], "stats": {}}

    def _save_state(self):
        try:
            self._state["log"] = (self._state.get("log") or [])[-_LOG_LIMIT:]
            self.save_data(_STATE_KEY, self._state)
        except Exception as err:  # noqa: BLE001
            logger.error(f"HrCleanup 保存状态失败：{err}")

    def _audit(self, entry: Dict[str, Any]):
        entry["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._state.setdefault("log", []).append(entry)
        # 删除记录单独留档，供「数据页」展示（演练命中不算删除，但也会记录动作）
        if entry.get("action") in ("deleted", "dry_run"):
            records = self._state.setdefault("records", [])
            records.append(
                {
                    "time": entry["time"],
                    "action": entry.get("action"),
                    "hash": entry.get("hash", ""),
                    "title": entry.get("title", ""),
                    "downloader": entry.get("downloader_name") or entry.get("downloader") or "",
                    "seed_hours": entry.get("seed_hours"),
                    "required_hours": entry.get("required_hours"),
                    "file_count": entry.get("file_count"),
                    "dest_storage": entry.get("dest_storage", ""),
                    "reason": entry.get("reason", ""),
                }
            )
            if len(records) > _RECORD_LIMIT:
                del records[: len(records) - _RECORD_LIMIT]
        # 统计
        stats = self._state.setdefault("stats", {})
        stats["last_run"] = entry["time"]
        action = entry.get("action")
        if action == "deleted":
            stats["deleted_total"] = int(stats.get("deleted_total", 0)) + 1
        elif action == "dry_run":
            stats["dry_run_total"] = int(stats.get("dry_run_total", 0)) + 1
        elif action == "skip":
            stats["skipped_total"] = int(stats.get("skipped_total", 0)) + 1

    def _in_cooldown(self, torrent_hash: str) -> bool:
        if self._retry_cooldown_hours <= 0:
            return False
        ts = (self._state.get("failures") or {}).get(torrent_hash)
        when = _dt(ts)
        if not when:
            return False
        return datetime.now() - when < timedelta(hours=self._retry_cooldown_hours)

    def _mark_failure(self, torrent_hash: str):
        self._state.setdefault("failures", {})[torrent_hash] = datetime.now().isoformat()

    def _clear_failure(self, torrent_hash: str):
        try:
            (self._state.get("failures") or {}).pop(torrent_hash, None)
        except Exception:  # noqa: BLE001
            pass

    # ==================================================================
    # 阈值计算
    # ==================================================================
    def _required_hours(self) -> float:
        """生效阈值（小时）= max(做种硬下限, H&R 时长 + 额外时长)。"""
        return float(max(self._min_seed_hours, self._hr_hours + self._extra_hours))

    # ==================================================================
    # 目标存储（115）识别
    # ==================================================================
    def _candidate_dest_storages(self) -> List[str]:
        """返回可接受的「115 目标存储」标识集合。

        规则：配置值优先；再自动从系统 Storages 里挑出 type 或 name 含 "115"
        （或等于 u115）的项。这样既兼容内置 u115，也兼容插件提供的
        「115网盘Plus」等自定义存储类型。
        """
        if self._dest_types_memo is not None:
            return self._dest_types_memo
        out: List[str] = []
        configured = str(self._dest_storage or "").strip()
        if configured:
            out.append(configured)
        confs: List[Any] = []
        try:
            from app.sdk.services import StorageHelper  # type: ignore

            confs = StorageHelper().get_storagies() or []
        except Exception:  # noqa: BLE001
            try:
                from app.application.storage import StorageHelper  # type: ignore

                confs = StorageHelper().get_storagies() or []
            except Exception:  # noqa: BLE001
                confs = []
        if not confs and SystemConfigKey is not None:
            try:
                confs = self.systemconfig.get(SystemConfigKey.Storages) or []
            except Exception:  # noqa: BLE001
                confs = []
        for conf in confs:
            if isinstance(conf, dict):
                values = [conf.get("type"), conf.get("name")]
            else:
                values = [getattr(conf, "type", None), getattr(conf, "name", None)]
            for val in values:
                if not val:
                    continue
                text = str(val).strip()
                if text and (text.lower() == "u115" or "115" in text) and text not in out:
                    out.append(text)
        if not out:
            out.append("u115")
        self._dest_types_memo = out
        return out

    def _dest_label(self) -> str:
        return "/".join(self._candidate_dest_storages())

    def _is_dest_storage(self, value: Any) -> bool:
        """判断整理记录的 dst_storage 是否指向 115。"""
        text = str(value or "").strip()
        if not text:
            return False
        return text in self._candidate_dest_storages()

    # ==================================================================
    # 路径辅助
    # ==================================================================
    def _extra_allow(self) -> List[str]:
        return [ln.strip() for ln in str(self._extra_allow_paths or "").splitlines() if ln.strip()]

    def _get_download_dirs(self) -> List[str]:
        paths: List[str] = []
        # 1) DirectoryHelper（V3）
        for mod, cls_name in (
            ("app.application.directory", "DirectoryHelper"),
            ("app.helper.directory", "DirectoryHelper"),
        ):
            try:
                module = __import__(mod, fromlist=[cls_name])
                helper_cls = getattr(module, cls_name)
                for item in helper_cls().get_dirs() or []:
                    p = getattr(item, "download_path", None)
                    if not p and isinstance(item, dict):
                        p = item.get("download_path")
                    if p:
                        paths.append(str(p))
                if paths:
                    break
            except Exception:  # noqa: BLE001
                continue
        # 2) 系统配置兜底
        if not paths and SystemConfigKey is not None:
            try:
                raw = self.systemconfig.get(SystemConfigKey.Directories) or []
                for item in raw:
                    p = None
                    if isinstance(item, dict):
                        p = item.get("download_path")
                    else:
                        p = getattr(item, "download_path", None)
                    if p:
                        paths.append(str(p))
            except Exception as err:  # noqa: BLE001
                logger.warning(f"HrCleanup 读取系统下载目录失败：{err}")
        # 去重
        return sorted({p.rstrip("/") for p in paths if p})

    def _get_library_dirs(self) -> List[str]:
        libs: List[str] = []
        try:
            from app.sdk.services import MediaServerHelper  # type: ignore

            configs = MediaServerHelper().get_configs() or {}
            for _name, conf in configs.items():
                for key in ("library_path", "libraries", "media_library", "sync_libraries"):
                    val = getattr(conf, key, None)
                    if isinstance(val, str) and val.strip():
                        libs.append(val.strip())
                    elif isinstance(val, (list, tuple)):
                        for one in val:
                            if isinstance(one, str) and one.strip():
                                libs.append(one.strip())
                            elif isinstance(one, dict) and one.get("path"):
                                libs.append(str(one["path"]).strip())
        except Exception:  # noqa: BLE001
            pass
        return sorted({p.rstrip("/") for p in libs if p})

    def _check_paths(self, sources: List[str]) -> Tuple[bool, str]:
        if not sources:
            return False, "无源文件路径"
        allowed = self._get_download_dirs() + self._extra_allow()
        if not allowed:
            return False, "未配置下载目录（DirectoryHelper 为空），拒绝删除"
        libraries = self._get_library_dirs()
        for src in sources:
            if not any(_is_under(src, root) for root in allowed):
                return False, f"源文件不在下载目录内：{src}"
            for lib in libraries:
                if _is_under(src, lib):
                    return False, f"源文件位于媒体库目录内，拒绝删除：{src}"
        return True, ""

    # ==================================================================
    # 数据采集
    # ==================================================================
    def _iter_download_history(self, only_hash: Optional[str] = None):
        """按日期倒序遍历下载历史，遇超出 scan_days 即停止。"""
        if list_download_history is None:
            logger.error("HrCleanup 无法导入 list_download_history，跳过扫描")
            return
        cutoff = datetime.now() - timedelta(days=self._scan_days)
        filters: Dict[str, Any] = {}
        if only_hash:
            filters["download_hash"] = only_hash
        page_no = 1
        while page_no <= 100:
            try:
                page = list_download_history(
                    filters or None,
                    {"page": page_no, "count": 200, "sort": {"field": "date", "direction": "desc"}},
                )
            except Exception as err:  # noqa: BLE001
                logger.error(f"HrCleanup 查询下载历史失败（第{page_no}页）：{err}")
                return
            items = getattr(page, "items", None) or []
            for item in items:
                when = _dt(getattr(item, "date", None))
                if when and when < cutoff:
                    return
                yield item
            if not getattr(page, "has_next", False):
                return
            page_no += 1

    def _get_transfer_history(self, torrent_hash: str) -> List[Any]:
        """取该 hash 下所有「成功 + 目标存储匹配」的整理历史。"""
        if list_transfer_history is None:
            return []
        result: List[Any] = []
        page_no = 1
        while page_no <= 50:
            try:
                page = list_transfer_history(
                    {"download_hash": torrent_hash},
                    {"page": page_no, "count": 100},
                )
            except Exception as err:  # noqa: BLE001
                logger.error(f"HrCleanup 查询整理历史失败（{torrent_hash}）：{err}")
                return result
            for item in getattr(page, "items", None) or []:
                dest_storage = str(getattr(item, "dest_storage", "") or "")
                if not self._is_dest_storage(dest_storage):
                    continue
                if getattr(item, "status", None) is not True:
                    continue
                result.append(item)
            if not getattr(page, "has_next", False):
                break
            page_no += 1
        return result

    def _subscription_index(self) -> List[Tuple[str, Optional[str], Optional[Any]]]:
        """构建订阅索引 [(名称, 年份, 季)]，用于判断下载是否来自订阅。"""
        index: List[Tuple[str, Optional[str], Optional[Any]]] = []
        if list_subscriptions is None:
            return index
        page_no = 1
        while page_no <= 20:
            try:
                page = list_subscriptions(None, {"page": page_no, "count": 200})
            except Exception as err:  # noqa: BLE001
                logger.warning(f"HrCleanup 查询订阅失败：{err}")
                break
            for sub in getattr(page, "items", None) or []:
                name = str(getattr(sub, "name", "") or "").strip()
                if not name:
                    continue
                year = getattr(sub, "year", None)
                season = getattr(sub, "season", None)
                index.append((name, str(year) if year else None, season))
            if not getattr(page, "has_next", False):
                break
            page_no += 1
        return index

    @staticmethod
    def _is_subscribed(item: Any, index: List[Tuple[str, Optional[str], Optional[Any]]]) -> bool:
        title = str(getattr(item, "title", "") or "")
        item_year = getattr(item, "year", None)
        # note 中带订阅标记（MoviePilot 实际写入 {"source": "Subscribe|{...}"}）
        note = getattr(item, "note", None)
        if isinstance(note, dict):
            for key, val in note.items():
                if "subscrib" in str(key).lower():
                    return True
                if isinstance(val, str) and "subscrib" in val.lower():
                    return True
        channel = str(getattr(item, "channel", "") or "").lower()
        if "subscrib" in channel or channel in ("subscribe", "subscription"):
            return True
        if not title or not index:
            return False
        for name, year, _season in index:
            if not name:
                continue
            if name in title or title in name:
                if year and item_year and str(year) != str(item_year):
                    continue
                return True
        return False

    # ==================================================================
    # 下载器交互
    # ==================================================================
    def _downloader_instances(self, preferred: Optional[str]) -> List[Tuple[Optional[str], Any]]:
        out: List[Tuple[Optional[str], Any]] = []
        if DownloaderHelper is None:
            return out
        try:
            helper = DownloaderHelper()
        except Exception as err:  # noqa: BLE001
            logger.warning(f"HrCleanup 初始化 DownloaderHelper 失败：{err}")
            return out
        if preferred:
            try:
                svc = helper.get_service(preferred)
                if svc and getattr(svc, "instance", None):
                    out.append((preferred, svc.instance))
            except Exception:  # noqa: BLE001
                pass
        try:
            for name, svc in (helper.get_services() or {}).items():
                inst = getattr(svc, "instance", None)
                if inst and all(inst is not existing for _n, existing in out):
                    out.append((name, inst))
        except Exception as err:  # noqa: BLE001
            logger.warning(f"HrCleanup 枚举下载器失败：{err}")
        return out

    def _find_torrent(self, torrent_hash: str, preferred: Optional[str]):
        """返回 (torrent_dict | None, instance | None, downloader_name | None, queried_ok)。

        queried_ok 表示是否至少有一个下载器**成功应答了查询**。用于区分两种情况：
        - queried_ok=True 且没查到 → 种子确实已不在下载器（已停止做种）
        - queried_ok=False        → 所有下载器都连不上，状态未知（不能当作已完成）
        """
        queried_ok = False
        for name, inst in self._downloader_instances(preferred):
            try:
                data = inst.get_torrents(ids=torrent_hash)
            except Exception:  # noqa: BLE001
                continue
            queried_ok = True
            torrents = None
            if isinstance(data, tuple):
                torrents = data[0]
            else:
                torrents = data
            if torrents:
                try:
                    return torrents[0], inst, name, True
                except Exception:  # noqa: BLE001
                    return torrents, inst, name, True
        return None, None, None, queried_ok

    @staticmethod
    def _torrent_field(torrent: Any, *names: str) -> Any:
        """兼容 dict / 对象 / qbittorrent-api TorrentDictionary 的字段读取。"""
        for name in names:
            try:
                if isinstance(torrent, dict) and name in torrent:
                    return torrent[name]
                val = getattr(torrent, name, None)
                if val is not None:
                    return val
            except Exception:  # noqa: BLE001
                continue
        try:
            return torrent.get(names[0])  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return None

    def _seeding_seconds(self, torrent: Any) -> Tuple[Optional[float], str]:
        """从下载器取「做种时长（秒）」。

        优先用下载器原生字段 seeding_time（qbittorrent 的"已做种"时长）；
        Transmission 用 secondsSeeding；rTorrent 无该字段时用完成时间估算。
        """
        for name in ("seeding_time", "seeding_time_seconds", "secondsSeeding", "seedingSeconds"):
            val = self._torrent_field(torrent, name)
            if val is None:
                continue
            try:
                return float(val), f"下载器字段 {name}"
            except Exception:  # noqa: BLE001
                continue
        # 兜底：用完成时间/添加时间估算
        now = time.time()
        for name in ("completion_on", "doneDate", "completed_time"):
            val = self._torrent_field(torrent, name)
            try:
                done = float(val)
            except Exception:  # noqa: BLE001
                continue
            if done > 0:
                return max(0.0, now - done), f"由 {name} 估算"
        for name in ("added_on", "addedDate"):
            val = self._torrent_field(torrent, name)
            try:
                added = float(val)
            except Exception:  # noqa: BLE001
                continue
            if added > 0:
                return max(0.0, now - added), f"由 {name} 估算"
        return None, "下载器未提供做种时长"

    def _check_seeding(self, torrent: Optional[Any], queried_ok: bool = True) -> Tuple[bool, str, float]:
        """以下载器报告的做种时长为准判定是否达到 H&R 阈值。"""
        required = self._required_hours()
        if torrent is None:
            if not queried_ok:
                # 下载器连不上，无法判断种子状态 → 未知，保守跳过
                return False, f"无法连接下载器查询种子，状态未知（阈值 {required:.0f}h），跳过", -1.0
            if str(self._missing_torrent_policy or "").lower() == "skip":
                return False, f"种子已不在下载器，无法校验做种时长（策略 skip，阈值 {required:.0f}h）", -1.0
            # assume_done：种子已从下载器移除（已停止做种），视为做种义务结束
            return True, "种子已不在下载器，按「视为已达标」策略放行", -1.0
        seeding, how = self._seeding_seconds(torrent)
        if seeding is None:
            # 下载器不提供做种时长 → 无法判定 → 保守跳过
            return False, f"无法从下载器读取做种时长（阈值 {required:.0f}h），跳过", -1.0
        seed_hours = seeding / 3600.0
        if seed_hours + 1e-6 < required:
            return (
                False,
                f"做种 {seed_hours:.1f}h < 阈值 {required:.0f}h（{how}）",
                seed_hours,
            )
        return True, f"做种 {seed_hours:.1f}h ≥ 阈值 {required:.0f}h（{how}）", seed_hours

    def _confirm_copied(self, histories: List[Any]) -> Tuple[bool, str]:
        """确认「复制到 115 成功」。

        默认只以「媒体整理记录成功」为准（见 _get_transfer_history 已过滤
        status==True 且 dest_storage 匹配）。原因：115 会在阈值时间（36h）内
        把「最近接收/需整理文件」里的副本转移到正式目录，到清理时原 dest
        路径通常已不存在，若按原路径硬复核会导致永远删不掉。

        仅当用户显式开启 verify_remote 时，才额外按原路径向云端复核。
        """
        if not histories:
            return False, f"未找到成功的 {self._dest_label()} 整理记录"
        if not self._verify_remote:
            return True, f"整理记录成功（{len(histories)} 条），跳过云端路径复核"
        if StorageChain is None or FileItem is None:
            return False, "已开启云端复核，但无法导入 StorageChain/FileItem，拒绝删除"
        chain = StorageChain()
        for hist in histories:
            dest = str(getattr(hist, "dest", "") or "")
            storage = str(getattr(hist, "dest_storage", "") or self._dest_storage)
            if not dest:
                return False, "整理历史缺少目标路径"
            try:
                remote = chain.get_file_item_strict(storage, Path(dest))
            except Exception as err:  # noqa: BLE001
                # 查询未知（限流/鉴权/网络）→ 永不因查不到而删
                return False, f"云端查询失败（状态未知）：{type(err).__name__}: {err}"
            if remote is None:
                return False, f"云端确认不存在：{dest}"
            if self._verify_size:
                remote_size = getattr(remote, "size", None)
                dest_item = getattr(hist, "dest_fileitem", None) or {}
                src_item = getattr(hist, "src_fileitem", None) or {}
                hist_size = None
                if isinstance(dest_item, dict) and dest_item.get("size") is not None:
                    hist_size = dest_item.get("size")
                elif isinstance(src_item, dict) and src_item.get("size") is not None:
                    hist_size = src_item.get("size")
                if remote_size is not None and hist_size is not None:
                    try:
                        if int(remote_size) != int(hist_size):
                            return (
                                False,
                                f"大小不一致 remote={remote_size} hist={hist_size}：{dest}",
                            )
                    except Exception:  # noqa: BLE001
                        pass  # 无法比较则容错放行
        return True, f"云端复核通过（{len(histories)} 条）"

    # ==================================================================
    # 执行动作
    # ==================================================================
    def _delete_torrent(
        self,
        torrent_hash: str,
        torrent,
        instance,
        delete_files: bool = False,
    ) -> Tuple[bool, str]:
        """删除下载器中的种子任务。

        delete_files=True 时等价于 qb 的「同时删除已下载的文件」，
        由下载器自己删硬盘文件，这是最可靠的删除方式。
        """
        try:
            if instance is None:
                torrent, instance, _name, _ok = self._find_torrent(torrent_hash, None)
            if instance is not None:
                ok = instance.delete_torrents(delete_file=bool(delete_files), ids=torrent_hash)
                if ok is False:
                    return False, "下载器拒绝删除任务"
                if delete_files:
                    return True, "已通过下载器删除任务并同时删除已下载文件"
                return True, "已删除种子任务（delete_file=False）"
            if DownloadChain is not None:
                ok = DownloadChain().remove_downloading(torrent_hash)
                return bool(ok), "已通过 DownloadChain 移除下载任务（文件由插件清理）"
            return True, "种子已不存在，跳过删种"
        except Exception as err:  # noqa: BLE001
            return False, f"删种失败：{type(err).__name__}: {err}"

    def _local_exists(self, path: str) -> bool:
        try:
            return os.path.exists(path)
        except Exception:  # noqa: BLE001
            return True  # 判断不了就当它还在，交给后续兜底

    def _verify_local_deleted(self, sources: List[str]) -> Tuple[bool, List[str]]:
        """校验下载器是否真的把硬盘文件删干净，返回 (是否全清, 残留列表)。"""
        left = [s for s in sources if self._local_exists(s)]
        return (not left), left

    def _delete_local_files(self, sources: List[str]) -> Tuple[bool, str]:
        if StorageChain is None or FileItem is None:
            return False, "无法导入 StorageChain/FileItem"
        chain = StorageChain()

        def _is_dir(p: str) -> bool:
            try:
                return os.path.isdir(p)
            except Exception:  # noqa: BLE001
                return False

        # 先删文件、再删目录（目录由深到浅），避免非空目录删不掉
        files = [s for s in sources if not _is_dir(s)]
        dirs = sorted([s for s in sources if _is_dir(s)], key=len, reverse=True)
        ordered = files + dirs

        deleted = 0
        missing = 0
        errors: List[str] = []
        for src in ordered:
            try:
                if _is_dir(src):
                    item = FileItem(storage="local", path=src, type="dir")
                    ok = chain.delete_media_file(item)
                    if ok is False:
                        # 兜底：直接按文件项删（空目录/适配器差异）
                        ok = chain.delete_file(FileItem(storage="local", path=src, type="dir"))
                else:
                    item = FileItem(storage="local", path=src, type="file")
                    ok = chain.delete_file(item)
            except Exception as err:  # noqa: BLE001
                errors.append(f"{src}: {type(err).__name__}: {err}")
                continue
            if ok is None:
                missing += 1  # 已不存在或不支持，视为跳过
            elif ok:
                deleted += 1
            else:
                errors.append(f"{src}: 返回失败")
        if errors:
            return False, f"本地删除 {deleted} 项、缺失 {missing} 项，但有失败：{'; '.join(errors[:3])}"
        return True, f"本地文件删除 {deleted} 项，缺失/跳过 {missing} 项"

    def _delete_history(self, history_id: int, torrent_hash: str, sources: List[str]) -> Tuple[bool, str]:
        if DownloadHistoryOper is None:
            return False, "无法导入 DownloadHistoryOper"
        try:
            oper = DownloadHistoryOper()
        except Exception as err:  # noqa: BLE001
            return False, f"初始化 DownloadHistoryOper 失败：{err}"
        # 标记文件为已删除（state=0）
        try:
            files = oper.get_files_by_hash(torrent_hash, state=1) or []
        except Exception:  # noqa: BLE001
            files = []
        for one in files:
            fullpath = getattr(one, "fullpath", None)
            if fullpath:
                try:
                    oper.delete_file_by_fullpath(fullpath)
                except Exception:  # noqa: BLE001
                    pass
        for src in sources:
            try:
                oper.delete_file_by_fullpath(src)
            except Exception:  # noqa: BLE001
                pass
        # 删除下载历史条目
        try:
            oper.delete_history(int(history_id))
        except Exception as err:  # noqa: BLE001
            return False, f"删除下载历史失败：{type(err).__name__}: {err}"
        return True, "已删除下载历史条目"

    def _delete_transfer_histories(self, histories: List[Any]) -> Tuple[bool, str]:
        if not histories:
            return True, "无整理历史需要删除"
        try:
            from app.db.oper.transferhistory import TransferHistoryOper  # type: ignore
        except Exception:  # noqa: BLE001
            try:
                from app.db.transferhistory_oper import TransferHistoryOper  # type: ignore
            except Exception:  # noqa: BLE001
                return False, "无法导入 TransferHistoryOper"
        done = 0
        try:
            oper = TransferHistoryOper()
            for hist in histories:
                hid = getattr(hist, "id", None)
                if hid is None:
                    continue
                try:
                    oper.delete_history(int(hid))
                    done += 1
                except Exception:  # noqa: BLE001
                    pass
        except Exception as err:  # noqa: BLE001
            return False, f"删除整理历史失败：{err}"
        return True, f"已删除整理历史 {done} 条"

    # ==================================================================
    # 主流程
    # ==================================================================
    def _process_one(self, item: Any, dry_run: bool, sub_index: List[Tuple[str, Optional[str], Optional[Any]]]) -> Dict[str, Any]:
        rh = {
            "hash": str(getattr(item, "download_hash", "") or ""),
            "title": str(getattr(item, "title", "") or ""),
            "downloader": getattr(item, "downloader", None),
            "history_id": getattr(item, "id", None),
            "action": "skip",
            "reason": "",
            "seed_hours": None,
            "required_hours": round(self._required_hours(), 1),
            "file_count": 0,
            "dest_storage": self._dest_storage,
            "dest": "",
        }
        torrent_hash = rh["hash"]
        if not torrent_hash:
            rh["reason"] = "缺少 download_hash"
            return rh
        if self._only_subscribed and not self._is_subscribed(item, sub_index):
            rh["reason"] = "非订阅来源（已开启仅订阅）"
            return rh
        if self._in_cooldown(torrent_hash):
            rh["reason"] = f"失败冷却中（{self._retry_cooldown_hours}h）"
            return rh

        # G1 整理成功记录
        histories = self._get_transfer_history(torrent_hash)
        if not histories:
            rh["reason"] = f"未找到成功的 {self._dest_label()} 整理记录"
            return rh

        # G2 云端复核
        ok, msg = self._confirm_copied(histories)
        if not ok:
            rh["reason"] = f"G2 {msg}"
            return rh

        # 收集源文件路径
        sources: List[str] = []
        for hist in histories:
            for one in (getattr(hist, "files", None) or []):
                if isinstance(one, str) and one.strip():
                    sources.append(one.strip())
            src = getattr(hist, "src", None)
            if src and src not in sources:
                sources.append(str(src))
        sources = list(dict.fromkeys(sources))
        if not sources:
            rh["reason"] = "整理历史缺少源文件路径"
            return rh
        rh["file_count"] = len(sources)
        rh["dest"] = str(getattr(histories[0], "dest", "") or "")

        # G4 路径校验
        ok, msg = self._check_paths(sources)
        if not ok:
            rh["reason"] = f"G4 {msg}"
            return rh

        # G3 做种判定（以下载器报告的做种时长为准）
        torrent, instance, dl_name, queried_ok = self._find_torrent(torrent_hash, rh["downloader"])
        rh["downloader_name"] = dl_name or rh["downloader"]
        ok, msg, seed_hours = self._check_seeding(torrent, queried_ok)
        rh["seed_hours"] = None if seed_hours < 0 else round(seed_hours, 1)
        if not ok:
            rh["reason"] = f"G3 {msg}"
            return rh

        rh["gates"] = {"G1": True, "G2": True, "G3": True, "G4": True, "seed_note": msg}
        rh["sources"] = sources

        if dry_run:
            rh["action"] = "dry_run"
            rh["reason"] = f"演练：将会删除（{msg}）"
            return rh

        # ---- 真实执行：S1 删种(可带删文件) -> S1b 校验/兜底删本地 -> S3 删下载历史 ----
        use_files = bool(self._delete_with_files) and instance is not None
        ok, s1 = self._delete_torrent(torrent_hash, torrent, instance, delete_files=use_files)
        if not ok:
            rh["reason"] = f"S1 {s1}"
            self._mark_failure(torrent_hash)
            return rh

        # 删完校验硬盘文件是否真的没了；有残留则插件兜底删除
        left: List[str] = []
        s2 = ""
        if use_files:
            all_gone, left = self._verify_local_deleted(sources)
            if all_gone:
                s2 = "下载器已删除硬盘文件，校验通过"
            else:
                ok2, s2 = self._delete_local_files(left)
                if not ok2:
                    rh["reason"] = f"S1 {s1}；S2 兜底删除失败：{s2}"
                    self._mark_failure(torrent_hash)
                    return rh
                s2 = f"下载器删除后残留 {len(left)} 项，已兜底删除：{s2}"
        else:
            ok2, s2 = self._delete_local_files(sources)
            if not ok2:
                rh["reason"] = f"S1 {s1}；S2 {s2}"
                self._mark_failure(torrent_hash)
                return rh

        ok, s3 = self._delete_history(int(rh["history_id"]), torrent_hash, sources)
        if not ok:
            rh["reason"] = f"S1 {s1}；S2 {s2}；S3 {s3}"
            self._mark_failure(torrent_hash)
            return rh

        s4 = ""
        if self._delete_transfer_history:
            _ok4, s4 = self._delete_transfer_histories(histories)

        rh["action"] = "deleted"
        rh["reason"] = f"{s1}；{s2}；{s3}" + (f"；{s4}" if s4 else "")
        self._clear_failure(torrent_hash)
        return rh

    def _scan(self, only_hash: Optional[str] = None, force_dry_run: Optional[bool] = None) -> Dict[str, Any]:
        if list_download_history is None:
            return {"ok": False, "message": "宿主缺少 list_download_history，无法扫描", "results": []}
        dry_run = self._dry_run if force_dry_run is None else force_dry_run
        sub_index = self._subscription_index() if self._only_subscribed else []
        results: List[Dict[str, Any]] = []
        handled = 0
        for item in self._iter_download_history(only_hash):
            if handled >= self._max_per_run and not only_hash:
                break
            item_hash = str(getattr(item, "download_hash", "") or "")
            if not item_hash:
                continue
            # 同一 hash 多条下载历史只处理一次
            if any(r.get("hash") == item_hash for r in results):
                continue
            outcome = self._process_one(item, dry_run, sub_index)
            results.append(outcome)
            handled += 1
            self._audit(outcome)

        self._save_state()
        deleted = [r for r in results if r.get("action") == "deleted"]
        dry = [r for r in results if r.get("action") == "dry_run"]
        skipped = [r for r in results if r.get("action") == "skip"]
        summary = {
            "ok": True,
            "dry_run": dry_run,
            "required_hours": self._required_hours(),
            "total": len(results),
            "deleted": len(deleted),
            "dry_run_hits": len(dry),
            "skipped": len(skipped),
            "results": results,
        }
        if self._notify:
            has_action = bool(deleted or dry)
            if has_action or not self._notify_only_on_action:
                self._notify_summary(summary)
        logger.info(
            f"HrCleanup 扫描完成 dry_run={dry_run} 总计={len(results)} "
            f"删除={len(deleted)} 演练命中={len(dry)} 跳过={len(skipped)}"
        )
        return summary

    def _notify_summary(self, summary: Dict[str, Any]):
        try:
            lines = [
                f"模式：{'演练（dry-run）' if summary['dry_run'] else '实删'}",
                f"整理成功并复核通过的候选：{summary['deleted'] + summary['dry_run_hits']} 个",
                f"跳过：{summary['skipped']} 个（做种/复核/路径/来源不满足）",
            ]
            for r in summary["results"][:10]:
                if r.get("action") in ("deleted", "dry_run"):
                    lines.append(f"· [{r['action']}] {r.get('title') or r.get('hash')} — {r.get('reason')}")
            text = "\n".join(lines)
            kwargs: Dict[str, Any] = {"title": "H&R 做种清理", "text": text}
            if MessageType is not None and hasattr(MessageType, "Plugin"):
                kwargs["mtype"] = MessageType.Plugin
            self.post_message(**kwargs)
        except Exception as err:  # noqa: BLE001
            logger.warning(f"HrCleanup 发送通知失败：{err}")

    # ==================================================================
    # API（手动触发）
    # ==================================================================
    def api_run(self, hash: Optional[str] = None) -> Dict[str, Any]:
        return self._scan(only_hash=hash, force_dry_run=None)

    def api_preview(self, hash: Optional[str] = None) -> Dict[str, Any]:
        return self._scan(only_hash=hash, force_dry_run=True)

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/run",
                "endpoint": self.api_run,
                "methods": ["GET"],
                "summary": "立即扫描并执行（按当前 dry_run 配置）",
                "description": "按四闸门扫描下载历史并清理；可传 ?hash= 只处理单个种子",
            },
            {
                "path": "/preview",
                "endpoint": self.api_preview,
                "methods": ["GET"],
                "summary": "只判定不执行（强制演练）",
                "description": "返回本次将会删除的候选与跳过原因，不做任何删除",
            },
        ]

    # ==================================================================
    # 定时服务
    # ==================================================================
    def get_service(self) -> List[Dict[str, Any]]:
        try:
            trigger = CronTrigger.from_crontab(self._cron or "0 4 * * *")
        except Exception as err:  # noqa: BLE001
            logger.error(f"HrCleanup cron 表达式非法（{self._cron}）：{err}，回退 0 4 * * *")
            trigger = CronTrigger.from_crontab("0 4 * * *")
        return [
            {
                "id": "HrCleanup",
                "name": "H&R 做种清理",
                "trigger": trigger,
                "func": self._scheduled_run,
                "kwargs": {},
            }
        ]

    def _scheduled_run(self):
        if not self._enabled:
            return
        try:
            self._scan()
        except Exception as err:  # noqa: BLE001
            logger.error(f"HrCleanup 定时执行异常：{type(err).__name__}: {err}")

    # ==================================================================
    # 表单
    # ==================================================================
    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        defaults = {
            "enabled": self._enabled,
            "dry_run": self._dry_run,
            "cron": self._cron,
            "min_seed_hours": self._min_seed_hours,
            "hr_hours": self._hr_hours,
            "extra_hours": self._extra_hours,
            "dest_storage": self._dest_storage,
            "only_subscribed": self._only_subscribed,
            "max_per_run": self._max_per_run,
            "scan_days": self._scan_days,
            "verify_remote": self._verify_remote,
            "verify_size": self._verify_size,
            "delete_with_files": self._delete_with_files,
            "missing_torrent_policy": self._missing_torrent_policy,
            "delete_transfer_history": self._delete_transfer_history,
            "extra_allow_paths": self._extra_allow_paths,
            "notify": self._notify,
            "notify_only_on_action": self._notify_only_on_action,
            "retry_cooldown_hours": self._retry_cooldown_hours,
        }

        def col(inner: Dict[str, Any], md: int = 6) -> Dict[str, Any]:
            return {"component": "VCol", "props": {"cols": 12, "md": md}, "content": [inner]}

        def switch(key: str, label: str) -> Dict[str, Any]:
            return {
                "component": "VSwitch",
                "props": {"model": key, "label": label, "color": "primary", "persistent-hint": True},
            }

        def text_field(key: str, label: str, hint: str = "", ftype: str = "text") -> Dict[str, Any]:
            return {
                "component": "VTextField",
                "props": {"model": key, "label": label, "hint": hint, "type": ftype},
            }

        components: List[Dict[str, Any]] = [
            {
                "component": "VForm",
                "props": {"model": defaults},
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            col(switch("enabled", "启用插件"), 4),
                            col(switch("dry_run", "演练模式（只记录不删除）"), 4),
                            col(switch("notify", "发送通知"), 4),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            col(text_field("cron", "定时 Cron", "例：0 4 * * *（每天 04:00）"), 4),
                            col(text_field("min_seed_hours", "做种时长下限（小时）", "硬要求默认 36", "number"), 4),
                            col(text_field("hr_hours", "H&R 要求时长（小时）", "按站点填写，默认 24", "number"), 4),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            col(text_field("extra_hours", "H&R 额外等待（小时）", "默认 8", "number"), 4),
                            col(text_field("dest_storage", "目标存储类型（留空自动识别）", "留空自动识别含 115 的存储；也可填真实 type，如 115网盘Plus"), 4),
                            col(text_field("max_per_run", "单轮最多清理（个）", "默认 5", "number"), 4),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            col(text_field("scan_days", "回看下载历史天数", "默认 30", "number"), 4),
                            col(text_field("retry_cooldown_hours", "失败冷却（小时）", "默认 24", "number"), 4),
                            col(switch("only_subscribed", "只处理订阅来源"), 4),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            col(switch("delete_with_files", "由下载器「同时删除已下载的文件」"), 6),
                            col(switch("delete_transfer_history", "同时删除整理历史"), 6),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            col(
                                {
                                    "component": "VSelect",
                                    "props": {
                                        "model": "missing_torrent_policy",
                                        "label": "种子已不在下载器时",
                                        "items": [
                                            {"title": "视为已达标，允许清理", "value": "assume_done"},
                                            {"title": "保守跳过", "value": "skip"},
                                        ],
                                        "hint": "下载器里已找不到该种子（可能被其它插件删除）时如何处理。"
                                        "assume_done：任务已消失=已停止做种，删文件不会加剧 H&R，避免文件残留；"
                                        "skip：无法校验就跳过",
                                    },
                                },
                                12,
                            )
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            col(switch("verify_remote", "云端复核原路径是否存在（默认关）"), 4),
                            col(switch("verify_size", "云端复核比对大小（需同时开启左侧）"), 4),
                            col(switch("notify_only_on_action", "仅有删除时通知"), 4),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            col(
                                {
                                    "component": "VTextarea",
                                    "props": {
                                        "model": "extra_allow_paths",
                                        "label": "额外允许删除的路径（每行一个）",
                                        "rows": 3,
                                        "hint": "默认仅允许 DirectoryHelper 配置的下载目录",
                                    },
                                },
                                12,
                            )
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            col(
                                {
                                    "component": "VAlert",
                                    "props": {
                                        "type": "info",
                                        "variant": "tonal",
                                        "text": (
                                            "生效阈值 = max(做种下限, H&R 时长 + 额外等待)。"
                                            "默认 36 / 24 / 8 → 36 小时。复制成功以「媒体整理记录成功」为准，"
                                            "默认不做云端路径复核（115 会把副本转移到正式目录，原路径到清理时通常已不存在）。"
                                            "115 上的文件永不删除。"
                                        ),
                                    },
                                },
                                12,
                            )
                        ],
                    },
                ],
            }
        ]
        return components, defaults

    # ==================================================================
    # 详情页
    # ==================================================================
    def get_page(self) -> List[Dict[str, Any]]:
        run_label = "运行一次（实删）" if not self._dry_run else "运行一次（演练）"
        pages: List[Dict[str, Any]] = [
            # ---------------- 操作按钮 ----------------
            {
                "component": "VRow",
                "props": {"align": "center"},
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "warning" if not self._dry_run else "primary",
                                    "variant": "flat",
                                    "prependIcon": "mdi-play",
                                    "block": True,
                                },
                                "text": run_label,
                                "events": {
                                    "click": {
                                        "api": "plugin/HrCleanup/run",
                                        "method": "get",
                                    }
                                },
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "secondary",
                                    "variant": "tonal",
                                    "prependIcon": "mdi-eye-outline",
                                    "block": True,
                                },
                                "text": "仅预览（不删除）",
                                "events": {
                                    "click": {
                                        "api": "plugin/HrCleanup/preview",
                                        "method": "get",
                                    }
                                },
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "div",
                                "props": {"class": "text-caption text-medium-emphasis"},
                                "text": (
                                    "点击「运行一次」立即按当前配置扫描并处理一次（不受定时任务影响）。"
                                    + (
                                        "当前为演练模式，只会统计命中、不会真正删除；需要实删请先在配置里关闭 dry_run。"
                                        if self._dry_run
                                        else "当前为实删模式：会真正删除下载器种子、下载记录与硬盘下载文件（115 网盘副本保留）。"
                                    )
                                ),
                            }
                        ],
                    },
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "color": "primary"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "content": [
                                            {"component": "div", "text": f"状态：{'已启用' if self._enabled else '未启用'}"},
                                            {"component": "div", "text": f"模式：{'演练 dry-run' if self._dry_run else '实删'}"},
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "color": "teal"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "content": [
                                            {"component": "div", "text": f"生效阈值：{self._required_hours():.0f} 小时"},
                                            {"component": "div", "text": f"定时：{self._cron}"},
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "color": "indigo"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "content": [
                                            {"component": "div", "text": f"目标存储：{self._dest_label()}"},
                                            {"component": "div", "text": f"仅订阅：{'是' if self._only_subscribed else '否'}"},
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "color": "amber"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "content": [
                                            {"component": "div", "text": f"单轮上限：{self._max_per_run}"},
                                            {"component": "div", "text": f"回看：{self._scan_days} 天"},
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ]

        # ---------------- 数据页：累计统计 ----------------
        stats = self._state.get("stats") or {}
        pages.append(
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "color": "success"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "content": [
                                            {"component": "div", "text": f"已删除任务：{int(stats.get('deleted_total', 0))} 个"},
                                            {"component": "div", "text": f"演练命中：{int(stats.get('dry_run_total', 0))} 个"},
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "color": "grey"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "content": [
                                            {"component": "div", "text": f"累计跳过：{int(stats.get('skipped_total', 0))} 个"},
                                            {"component": "div", "text": f"最近运行：{stats.get('last_run') or '—'}"},
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "color": "deep-purple"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "content": [
                                            {"component": "div", "text": f"删除方式：{'下载器同时删文件' if self._delete_with_files else '插件删本地文件'}"},
                                            {"component": "div", "text": f"失败冷却：{self._retry_cooldown_hours} 小时"},
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "color": "blue-grey"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "content": [
                                            {"component": "div", "text": f"删除记录条数：{len(self._state.get('records') or [])}"},
                                            {"component": "div", "text": f"冷却中任务：{len(self._state.get('failures') or {})}"},
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        )

        # ---------------- 数据页：删除记录 ----------------
        records = list(reversed((self._state.get("records") or [])[-100:]))
        rec_rows: List[Dict[str, Any]] = []
        for rec in records:
            action = str(rec.get("action", ""))
            label = "已删除" if action == "deleted" else "演练命中"
            seed = rec.get("seed_hours")
            seed_text = "—" if seed is None else f"{seed}h"
            req = rec.get("required_hours")
            rec_rows.append(
                {
                    "component": "tr",
                    "content": [
                        {"component": "td", "text": str(rec.get("time", ""))},
                        {"component": "td", "text": label},
                        {"component": "td", "text": str(rec.get("title") or rec.get("hash") or "")},
                        {"component": "td", "text": str(rec.get("downloader") or "")},
                        {"component": "td", "text": seed_text},
                        {"component": "td", "text": "—" if req is None else f"{req}h"},
                        {"component": "td", "text": str(rec.get("file_count") or 0)},
                        {"component": "td", "text": str(rec.get("dest_storage") or "")},
                    ],
                }
            )
        pages.append(
            {
                "component": "VCard",
                "props": {"variant": "outlined"},
                "content": [
                    {"component": "VCardTitle", "text": "删除记录（最新 100 条）"},
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VTable",
                                "props": {"density": "compact"},
                                "content": [
                                    {
                                        "component": "thead",
                                        "content": [
                                            {
                                                "component": "tr",
                                                "content": [
                                                    {"component": "th", "text": "时间"},
                                                    {"component": "th", "text": "动作"},
                                                    {"component": "th", "text": "标题/种子"},
                                                    {"component": "th", "text": "下载器"},
                                                    {"component": "th", "text": "做种"},
                                                    {"component": "th", "text": "阈值"},
                                                    {"component": "th", "text": "文件数"},
                                                    {"component": "th", "text": "目标存储"},
                                                ],
                                            }
                                        ],
                                    },
                                    {"component": "tbody", "content": rec_rows},
                                ],
                            },
                            {
                                "component": "div",
                                "text": "「已删除」为真实删除；「演练命中」为 dry-run 下判定会删除、但未执行。",
                            },
                        ],
                    },
                ],
            }
        )

        # ---------------- 数据页：最近执行日志 ----------------
        log = list(reversed((self._state.get("log") or [])[-50:]))
        rows: List[Dict[str, Any]] = []
        for entry in log:
            rows.append(
                {
                    "component": "tr",
                    "content": [
                        {"component": "td", "text": str(entry.get("time", ""))},
                        {"component": "td", "text": str(entry.get("action", ""))},
                        {"component": "td", "text": str(entry.get("title") or entry.get("hash") or "")},
                        {"component": "td", "text": "" if entry.get("seed_hours") is None else f"{entry.get('seed_hours')}h"},
                        {"component": "td", "text": str(entry.get("reason", ""))},
                    ],
                }
            )

        pages.append(
            {
                "component": "VCard",
                "props": {"variant": "outlined"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "text": "最近执行记录（最新 50 条，含跳过原因）",
                    },
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VTable",
                                "props": {"density": "compact"},
                                "content": [
                                    {
                                        "component": "thead",
                                        "content": [
                                            {
                                                "component": "tr",
                                                "content": [
                                                    {"component": "th", "text": "时间"},
                                                    {"component": "th", "text": "动作"},
                                                    {"component": "th", "text": "标题/种子"},
                                                    {"component": "th", "text": "做种"},
                                                    {"component": "th", "text": "说明"},
                                                ],
                                            }
                                        ],
                                    },
                                    {"component": "tbody", "content": rows},
                                ],
                            }
                        ],
                    },
                ],
            }
        )
        return pages
