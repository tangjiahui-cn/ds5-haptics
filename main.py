# -*- coding: utf-8 -*-
"""
MHWild_PS5震动修复 —— Decky 插件后端（v0.4.0）

v0.4 模型：
  * 一份默认配置 + 每个游戏一份配置（字段一致）；
  * 监听所有已安装 Steam 游戏的启动/退出；
  * 启用震动：游戏内手动即时生效/恢复；未进游戏开启则进游戏自动启用（延迟 20 秒）；
  * 阶段：idle / waiting（进游戏等待 20 秒，全锁定）/ switching（三连，仅强度可调）/
          refreshing（刷新按钮，全锁定）；
  * 切换期间允许改强度，取最后一次的值。
"""

import asyncio
import glob as globmod
import json
import os
import re
import subprocess
import time

import decky

APPID_MHW = "2246340"
ENGINE = os.path.join(decky.DECKY_PLUGIN_DIR, "mhwild_haptics.py")
RUNTIME_DIR = "/run/user/1000"
DECK_USER = "deck"
DATA_DIR = "/home/deck/.local/share/mhwild-haptics"
STEAM_DIR = "/home/deck/.local/share/Steam"
STEAM_LOG_CANDIDATES = [
    os.path.join(STEAM_DIR, "logs", "console_log.txt"),
    "/home/deck/.steam/steam/logs/console_log.txt",
]

IGNORE_NAME = re.compile(
    r"Steam Linux Runtime|Steamworks Common Redistributables|Proton|SteamVR|"
    r"Steam Client|Steam Controller|Steam Play|Steam Audio",
    re.IGNORECASE,
)
WAIT_SECONDS = 20


# ---------------------------------------------------------------- 进程/引擎

def _deck_ids():
    import pwd
    try:
        pw = pwd.getpwnam(DECK_USER)
        return pw.pw_uid, pw.pw_gid
    except KeyError:
        return 1000, 1000


def _demote():
    uid, gid = _deck_ids()
    try:
        os.setgid(gid)
        os.setgroups([])
    except OSError:
        pass
    os.setuid(uid)


def _run_engine(gname, *args, timeout=90):
    cmd = [
        "/usr/bin/python3", ENGINE,
        "--data-dir", DATA_DIR,
        "--game-name", gname or "Monster Hunter Wilds",
        *args,
    ]
    env = dict(os.environ)
    env.update({
        "XDG_RUNTIME_DIR": RUNTIME_DIR,
        "HOME": "/home/deck",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={RUNTIME_DIR}/bus",
    })
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env=env, preexec_fn=_demote,
        )
        decky.logger.info(
            "engine %s -> rc=%s out=%r err=%r",
            args, r.returncode, r.stdout.strip()[:300], r.stderr.strip()[:300],
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:  # noqa: BLE001
        decky.logger.error("engine %s exception: %s", args, e)
        return 1, "", str(e)


def _json_out(gname, *args):
    code, out, err = _run_engine(gname, *args)
    if out:
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            pass
    return {"ok": code == 0, "output": out, "error": err}


# ---------------------------------------------------------------- Steam 游戏

def _steam_libraries():
    libs = [STEAM_DIR]
    vdf = os.path.join(STEAM_DIR, "steamapps", "libraryfolders.vdf")
    try:
        txt = open(vdf, encoding="utf-8", errors="ignore").read()
        for m in re.finditer(r'"path"\s+"([^"]+)"', txt):
            path = m.group(1).replace("\\\\", "\\")
            if path not in libs:
                libs.append(path)
    except OSError:
        pass
    return libs


def _parse_acf(path):
    try:
        txt = open(path, encoding="utf-8", errors="ignore").read()
    except OSError:
        return None
    m_id = re.search(r'"appid"\s+"(\d+)"', txt)
    m_name = re.search(r'"name"\s+"([^"]+)"', txt)
    m_dir = re.search(r'"installdir"\s+"([^"]+)"', txt)
    if not m_id or not m_name:
        return None
    return {
        "appid": m_id.group(1),
        "name": m_name.group(1),
        "installdir": m_dir.group(1) if m_dir else "",
    }


def _installed_games():
    games = {}
    for lib in _steam_libraries():
        for acf in globmod.glob(os.path.join(lib, "steamapps", "appmanifest_*.acf")):
            info = _parse_acf(acf)
            if not info or IGNORE_NAME.search(info["name"]):
                continue
            games[info["appid"]] = info
    return [games[k] for k in sorted(games, key=lambda x: games[x]["name"].lower())]


def _proc_game_appid(games):
    """扫描 /proc，按 installdir 路径匹配正在运行的游戏。"""
    by_dir = {g["installdir"]: g for g in games if g.get("installdir")}
    if not by_dir:
        return None
    common = os.path.join("steamapps", "common")
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return None
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read()
        except OSError:
            continue
        for inst_dir, g in by_dir.items():
            needle = os.path.join(common, inst_dir).encode()
            if needle in cmd:
                return g
    return None


# ---------------------------------------------------------------- 插件

class Plugin:
    _stop = False
    _phase = "idle"           # idle / waiting / switching / refreshing
    _wait_until = 0.0
    _current = None           # {"appid":..., "name":...}
    _games = []
    _desired_volume = 100
    _log_pos = 0
    _log_ok = False

    # ---------------- 基础设施 ----------------

    async def _engine_cmd(self, *args):
        gname = (self._current or {}).get("name") or ""
        code, out, err = await asyncio.to_thread(_run_engine, gname, *args)
        return {"ok": code == 0, "output": out, "error": err}

    async def _status(self):
        gname = (self._current or {}).get("name") or ""
        return await asyncio.to_thread(_json_out, gname, "status", "--json")

    async def _profiles(self):
        return await asyncio.to_thread(_json_out, "", "profiles", "--json")

    def _effective(self, profiles):
        if not isinstance(profiles, dict):
            profiles = {}
        cur = self._current
        if cur and cur["appid"] in profiles:
            prof = profiles[cur["appid"]]
            if prof.get("enabled", True):
                return cur["appid"], prof
        return "global", profiles.get("global") or {}

    async def get_ui_state(self):
        status = await self._status()
        profiles = await self._profiles()
        key, eff = self._effective(profiles)
        wait_left = max(0, int(self._wait_until - time.time())) if self._phase == "waiting" else 0
        return {
            "status": status,
            "profiles": profiles,
            "games": self._games,
            "current": self._current,
            "phase": self._phase,
            "wait_seconds": wait_left,
            "active_key": key,
            "effective": eff,
            "desired_volume": self._desired_volume,
            "wait_total": WAIT_SECONDS,
        }

    # ---------------- 手动操作 ----------------

    async def set_vibration_enabled(self, enabled):
        """启用震动开关（游戏内立即生效/恢复；未进游戏只保存）。"""
        if self._phase != "idle":
            return {"ok": False, "error": "当前处于等待/切换/刷新状态，操作已锁定"}
        profiles = await self._profiles()
        key, eff = self._effective(profiles)
        if not self._current:
            # 未进游戏：仅保存
            await self._engine_cmd("profile-set", key, "vibration_enabled",
                                   "true" if enabled else "false")
            return {"ok": True, "output": "已保存（进游戏后生效）", "enabled": enabled}

        await self._engine_cmd("profile-set", key, "vibration_enabled",
                               "true" if enabled else "false")
        if enabled:
            self._phase = "switching"
            self._desired_volume = eff.get("volume", 100)
            try:
                r = await self._apply_sequence(key)
                if r.get("ok"):
                    # 最后写入优先：切换结束后应用最新强度
                    await self._engine_cmd("volume", "set", str(self._desired_volume),
                                           "--profile", key)
                else:
                    await self._engine_cmd("restore", "--profile", key)
                    await self._engine_cmd("profile-set", key, "vibration_enabled", "false")
            finally:
                self._phase = "idle"
            return {"ok": r.get("ok", False), "output": r.get("output"), "error": r.get("error"),
                    "enabled": bool(r.get("ok"))}
        # 关闭：立即恢复
        self._phase = "switching"
        try:
            code, out, err = await asyncio.to_thread(
                _run_engine, (self._current or {}).get("name"), "restore", "--profile", key)
        finally:
            self._phase = "idle"
        return {"ok": code == 0, "output": out, "error": err, "enabled": False}

    async def set_volume(self, pct):
        pct = max(0, min(200, int(pct)))
        if self._phase in ("waiting", "refreshing"):
            return {"ok": False, "error": "当前处于等待/刷新状态，操作已锁定"}
        profiles = await self._profiles()
        key, eff = self._effective(profiles)
        self._desired_volume = pct
        await self._engine_cmd("profile-set", key, "volume", str(pct))
        if self._phase == "switching":
            # 切换中：只记录，切换结束会应用最后一次值
            return {"ok": True, "output": "已记录（切换结束后生效）", "volume": pct}
        code, out, err = await asyncio.to_thread(
            _run_engine, (self._current or {}).get("name"), "volume", "set", str(pct),
            "--profile", key)
        return {"ok": code == 0, "output": out, "error": err, "volume": pct}

    async def set_per_game_enabled(self, enabled):
        """按游戏配置文件开关（仅游戏内可用）。"""
        if self._phase != "idle":
            return {"ok": False, "error": "当前处于等待/切换/刷新状态，操作已锁定"}
        if not self._current:
            return {"ok": False, "error": "未检测到运行中的游戏"}
        appid = self._current["appid"]
        profiles = await self._profiles()
        if enabled and (not isinstance(profiles, dict) or appid not in profiles):
            await self._engine_cmd("profile-copy", "global", appid)
        await self._engine_cmd("profile-set", appid, "enabled", "true" if enabled else "false")
        profiles = await self._profiles()
        key, eff = self._effective(profiles)
        self._desired_volume = eff.get("volume", 100)
        return {"ok": True, "enabled": enabled}

    async def refresh_vibration(self):
        if self._phase != "idle":
            return {"ok": False, "error": "当前处于等待/切换/刷新状态，操作已锁定"}
        if not self._current:
            return {"ok": False, "error": "未检测到运行中的游戏"}
        profiles = await self._profiles()
        key, eff = self._effective(profiles)
        if not eff.get("vibration_enabled"):
            return {"ok": False, "error": "请先开启「启用震动」"}
        st = await self._status()
        if not (st or {}).get("sink_name"):
            return {"ok": False, "error": "未连接 DualSense 手柄"}
        self._phase = "refreshing"
        try:
            code, out, err = await asyncio.to_thread(
                _run_engine, self._current["name"], "refresh", "--timeout", "25")
        finally:
            self._phase = "idle"
        return {"ok": code == 0, "output": out, "error": err}

    async def _apply_sequence(self, key):
        """开启三连：apply -> restore -> apply。"""
        gname = (self._current or {}).get("name")
        r1 = await asyncio.to_thread(_run_engine, gname, "apply", "--profile", key,
                                     "--timeout", "20")
        if r1[0] != 0:
            await asyncio.to_thread(_run_engine, gname, "restore", "--profile", key)
            return {"ok": False, "output": r1[1], "error": r1[2]}
        await asyncio.to_thread(_run_engine, gname, "restore", "--profile", key)
        r2 = await asyncio.to_thread(_run_engine, gname, "apply", "--profile", key,
                                     "--timeout", "20")
        return {"ok": r2[0] == 0, "output": r2[1], "error": r2[2]}

    # ---------------- 生命周期 / 监视 ----------------

    async def _main(self):
        self._stop = False
        self._games = await asyncio.to_thread(_installed_games)
        decky.logger.info("DS5-Haptics backend started (v0.4.3), games=%d", len(self._games))
        # 清理上次异常退出可能残留的快照
        try:
            if os.path.exists(os.path.join(DATA_DIR, "state.json")):
                await asyncio.to_thread(_run_engine, "", "restore")
                decky.logger.info("cleaned leftover snapshot on startup")
        except Exception as e:  # noqa: BLE001
            decky.logger.error(f"cleanup snapshot failed: {e}")
        asyncio.create_task(self._watch())

    async def _unload(self):
        self._stop = True

    async def _watch(self):
        while not self._stop:
            try:
                game = self._detect_game()
                if game and (not self._current or game["appid"] != self._current["appid"]):
                    await self._on_game_start(game)
                elif not game and self._current:
                    await self._on_game_exit()
            except Exception as e:  # noqa: BLE001
                decky.logger.error(f"watch error: {e}")
            await asyncio.sleep(2)

    def _detect_game(self):
        game = _proc_game_appid(self._games)
        if game:
            return game
        # 兜底：Steam 日志（最近一次 added 的游戏，若进程仍存在）
        appid = self._tail_steam_log()
        if appid:
            for g in self._games:
                if g["appid"] == appid and _proc_game_appid([g]):
                    return g
        return None

    def _tail_steam_log(self):
        path = None
        for cand in STEAM_LOG_CANDIDATES:
            if os.path.exists(cand):
                path = cand
                break
        if not path:
            return None
        try:
            size = os.path.getsize(path)
            if size < self._log_pos:
                self._log_pos = 0
            with open(path, encoding="utf-8", errors="ignore") as f:
                f.seek(self._log_pos)
                chunk = f.read()
                self._log_pos = f.tell()
        except OSError:
            return None
        last = None
        for m in re.finditer(r"Game process (added|removed)\s*: AppID (\d+)", chunk):
            last = m.group(2) if m.group(1) == "added" else None
        return last

    async def _on_game_start(self, game):
        self._current = {"appid": game["appid"], "name": game["name"]}
        self._phase = "idle"
        decky.logger.info("game start: %s (%s)", game["name"], game["appid"])
        profiles = await self._profiles()
        key, eff = self._effective(profiles)
        self._desired_volume = eff.get("volume", 100)
        if not eff.get("vibration_enabled"):
            return
        # 自动启用：等 2ch 音频流出现，再延迟 WAIT_SECONDS
        self._phase = "waiting"
        self._wait_until = time.time() + WAIT_SECONDS
        for _ in range(30):
            if not self._current:
                break
            st = await self._status()
            streams = (st or {}).get("streams") or []
            if any(s.get("channels") == 2 for s in streams):
                self._wait_until = time.time() + WAIT_SECONDS
                break
            await asyncio.sleep(2)
        while self._phase == "waiting" and time.time() < self._wait_until and not self._stop:
            await asyncio.sleep(1)
        if self._phase != "waiting" or not self._current:
            return
        self._phase = "switching"
        try:
            r = await self._apply_sequence(key)
            if r.get("ok"):
                await self._engine_cmd("volume", "set", str(self._desired_volume),
                                       "--profile", key)
            else:
                await self._engine_cmd("restore", "--profile", key)
                await self._engine_cmd("profile-set", key, "vibration_enabled", "false")
            decky.logger.info("auto enable: %s", r.get("ok"))
        finally:
            self._phase = "idle"

    async def _on_game_exit(self):
        decky.logger.info("game exit: %s", (self._current or {}).get("name"))
        try:
            st = await self._status()
            if isinstance(st, dict) and st.get("snapshot"):
                profiles = await self._profiles()
                key, _ = self._effective(profiles)
                await asyncio.to_thread(
                    _run_engine, (self._current or {}).get("name"), "restore", "--profile", key)
                decky.logger.info("auto restore on exit")
        except Exception as e:  # noqa: BLE001
            decky.logger.error(f"restore on exit failed: {e}")
        self._current = None
        self._phase = "idle"
        self._wait_until = 0.0
