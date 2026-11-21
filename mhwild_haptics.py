#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MHWild_PS5 触觉/震动 —— 核心引擎（阶段 1 CLI）

设计要点（按用户确认的规则）：
  * 只对 Monster Hunter Wilds（AppID 2246340）做触觉流修复；
  * 自动修复默认关闭，开启后才会在下次游戏启动时执行；
  * 音量不写死在代码里：从 profile 读取（profile 由用户在 UI/CLI 里设置，
    "从系统读取" 可把当前系统值同步进来）；
  * 游戏退出时恢复到"默认音频大小"（global profile 的音量，默认 100%）；
  * 游戏声音固定输出 HDMI；
  * 不做任何 root 操作（UCM 配置只检测并提示）。

用法示例：
  python3 mhwild_haptics.py status
  python3 mhwild_haptics.py fix            # 立即修复（手动按钮）
  python3 mhwild_haptics.py apply          # 按当前 profile 应用（供自动模式调用）
  python3 mhwild_haptics.py restore        # 退出游戏时恢复默认
  python3 mhwild_haptics.py volume read
  python3 mhwild_haptics.py volume set 130
  python3 mhwild_haptics.py profiles
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------- 常量

APPID_MHW = "2246340"
GAME_PROC_MARK = b"MonsterHunterWilds.exe"
GAME_APP_NAMES = ("Monster Hunter Wilds", "MonsterHunterWilds")
DS5_SINK_MATCH = "dualsense"

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "mhwild-haptics"

UCM_DIR = Path("/usr/share/alsa/ucm2/USB-Audio/Sony")
UCM_FILES = ("DualSense-PS5.conf", "DualSense-PS5-HiFi.conf")

PROFILE_DEFAULTS = {
    "enabled": True,            # 游戏配置：是否启用"按游戏配置文件"
    "vibration_enabled": False,  # 启用震动（默认关；开启后进游戏自动启用）
    "fix": True,                # 是否尝试建立 4 声道触觉流
    "volume": 100,              # 震动强度（%）
    "audio_out": "hdmi",        # 固定 HDMI
}

# 当前操作的运行中游戏名（CLI 用 --game-name 指定）
GAME_NAME = "Monster Hunter Wilds"

STATE_FILE = "state.json"  # 记录"进入游戏前"的快照，用于退出时恢复


# ---------------------------------------------------------------- 基础工具

def run(cmd, timeout=10):
    """执行命令，返回 (returncode, stdout, stderr)。"""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return r.returncode, r.stdout, r.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 1, "", str(e)


def pactl_json(*args):
    code, out, err = run(["pactl", "--format=json", *args])
    if code != 0:
        raise RuntimeError(f"pactl {' '.join(args)} 失败: {err.strip()}")
    return json.loads(out) if out.strip() else []


def pactl(*args):
    code, out, err = run(["pactl", *args])
    if code != 0:
        raise RuntimeError(f"pactl {' '.join(args)} 失败: {err.strip()}")
    return out.strip()


def channel_count(sample_spec):
    m = re.search(r"(\d+)ch", sample_spec or "")
    return int(m.group(1)) if m else 0


def get_default_sink():
    return pactl("get-default-sink")


def set_default_sink(name):
    pactl("set-default-sink", name)


# ---------------------------------------------------------------- 设备/流查询

def list_sinks():
    return pactl_json("list", "sinks") or []


def list_sink_inputs():
    return pactl_json("list", "sink-inputs") or []


def find_dualsense_sink():
    """找到手柄的 4 声道 sink。"""
    cands = [
        s for s in list_sinks()
        if DS5_SINK_MATCH in (s.get("name", "") + s.get("description", "")).lower()
    ]
    if not cands:
        return None
    # 优先 4 声道（analog-surround-40），其次任意
    for s in cands:
        if channel_count(s.get("sample_specification")) == 4:
            return s
    return cands[0]


def find_hdmi_sink(fallback_name=None):
    """找到 HDMI 输出 sink。"""
    for s in list_sinks():
        if "hdmi" in s.get("name", "").lower():
            return s
    if fallback_name:
        for s in list_sinks():
            if s.get("name") == fallback_name:
                return s
    return None


def _alnum(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def game_streams(inputs=None, game_name=None):
    """返回当前游戏的音频流列表（含 2ch / 4ch）。

    匹配规则（容错 Wine/Proton 的 app name 差异）：
      1) 原字符串（小写）相等 / 互相包含；
      2) 去掉空格和标点后（alnum）互相包含或前 12 位相同，例如
         "Monster Hunter Rise" ↔ "MonsterHunterRise.exe"。
    """
    raw_name = game_name or GAME_NAME or ""
    name = raw_name.lower()
    norm_name = _alnum(raw_name)
    inputs = inputs if inputs is not None else list_sink_inputs()
    out = []
    for si in inputs:
        app = ((si.get("properties") or {}).get("application.name", "") or "").lower()
        if not app or not name:
            continue
        norm_app = _alnum(app)
        matched = app == name or name in app or app in name
        if not matched and norm_name and norm_app:
            if norm_name in norm_app or norm_app in norm_name:
                matched = True
            elif len(norm_name) >= 12 and len(norm_app) >= 12 \
                    and norm_name[:12] == norm_app[:12]:
                matched = True
        if matched:
            si["_channels"] = channel_count(si.get("sample_specification"))
            out.append(si)
    return out


# 兼容旧名
def mh_streams(inputs=None):
    return game_streams(inputs)


def find_stream(streams, channels):
    for s in streams:
        if s.get("_channels") == channels:
            return s
    return None


def sink_by_index(idx):
    for s in list_sinks():
        if s.get("index") == idx:
            return s
    return None


def sink_name_by_index(idx):
    s = sink_by_index(idx)
    return s.get("name") if s else None


# ---------------------------------------------------------------- 音量

def sink_volumes(sink):
    """返回 {声道名: 原始值}，按 channel map 顺序。"""
    vol = sink.get("volume") or {}
    cmap = sink.get("channel_map") or ""
    order = [c.strip() for c in cmap.split(",") if c.strip()]
    if not order:
        order = list(vol.keys())
    return {ch: vol.get(ch, {}).get("value", 65536) for ch in order}


def sink_volume_percent(sink):
    vol = sink.get("volume") or {}
    cmap = sink.get("channel_map") or ""
    order = [c.strip() for c in cmap.split(",") if c.strip()] or list(vol.keys())
    return {
        ch: vol.get(ch, {}).get("value_percent", "?") for ch in order
    }


def set_haptic_volume(sink, percent):
    """
    设置手柄 RL/RR 声道音量为 percent（%），FL/FR 保持当前值。
    返回设置后的 {声道: 百分比}。
    """
    vols = sink_volumes(sink)
    cmap = [c.strip() for c in (sink.get("channel_map") or "").split(",") if c.strip()]
    raw = min(max(int(round(65536 * percent / 100.0)), 0), 65536 * 3)
    values = []
    for ch in cmap:
        if ch in ("rear-left", "rear-right", "aux2", "aux3"):
            values.append(raw)
        else:
            values.append(vols.get(ch, 65536))
    if not values:
        raise RuntimeError("无法解析手柄声道")
    pactl("set-sink-volume", sink["name"], *[str(v) for v in values])
    return percent


# ---------------------------------------------------------------- 游戏检测

def game_running():
    """扫描 /proc 判断荒野是否在跑（不依赖 pgrep 的输出格式）。"""
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            with open(f"/proc/{p}/cmdline", "rb") as f:
                cmd = f.read()
        except (IOError, OSError):
            continue
        if GAME_PROC_MARK in cmd:
            return True
    return False


# ---------------------------------------------------------------- profile

def data_dir(args):
    d = Path(args.data_dir).expanduser() if args.data_dir else DEFAULT_DATA_DIR
    (d / "profiles").mkdir(parents=True, exist_ok=True)
    (d / "logs").mkdir(parents=True, exist_ok=True)
    return d


def profile_path(dd, key):
    return dd / "profiles" / f"{key}.json"


def load_profile(dd, key):
    p = profile_path(dd, key)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    else:
        data = {}
    prof = dict(PROFILE_DEFAULTS)
    prof.update({k: v for k, v in data.items() if k in PROFILE_DEFAULTS})
    return prof


def save_profile(dd, key, prof):
    p = profile_path(dd, key)
    p.write_text(json.dumps(prof, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def state_path(dd):
    return dd / STATE_FILE


def load_state(dd):
    """读取"进入游戏前"的快照；不存在/损坏返回 None。"""
    p = state_path(dd)
    if not p.exists():
        return None
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
        return st if isinstance(st, dict) and st.get("in_game") else None
    except Exception:
        return None


def save_state(dd, st):
    state_path(dd).write_text(
        json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def clear_state(dd):
    p = state_path(dd)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass


def snapshot_system(dd, appid=APPID_MHW):
    """
    在"改动系统"之前记录当前状态：
      手柄 sink 各声道的原始音量值 + 当前默认输出设备。
    游戏退出时据此恢复到"进入游戏前"的值。
    """
    if load_state(dd):
        return  # 已存在快照（本局游戏已经记录过），不要覆盖
    sink = find_dualsense_sink()
    st = {
        "in_game": True,
        "appid": appid,
        "snapshot": {
            "default_sink": get_default_sink(),
            "sink_name": sink["name"] if sink else None,
            "channel_map": sink.get("channel_map") if sink else None,
            "volume_raw": sink_volumes(sink) if sink else None,
        },
    }
    save_state(dd, st)
    return st


def restore_system(dd):
    """
    恢复到"进入游戏前"的状态：
      - 手柄各声道音量恢复为快照值；
      - 默认输出恢复为快照值。
    """
    st = load_state(dd)
    sink = find_dualsense_sink()
    msgs = []

    if st and sink:
        snap = st.get("snapshot") or {}
        raw = snap.get("volume_raw") or {}
        cmap = [c.strip() for c in (sink.get("channel_map") or "").split(",") if c.strip()]
        vals = [raw.get(ch) for ch in cmap]
        if cmap and all(isinstance(v, int) for v in vals):
            pactl("set-sink-volume", sink["name"], *[str(v) for v in vals])
            msgs.append(f"手柄音量恢复为进入游戏前的值 {vals}")
        else:
            g = load_profile(dd, "global")
            set_haptic_volume(sink, g["volume"])
            msgs.append(f"快照不完整，手柄 RL/RR 恢复为 global {g['volume']}%")
    elif sink:
        g = load_profile(dd, "global")
        set_haptic_volume(sink, g["volume"])
        msgs.append(f"无游戏前快照，手柄 RL/RR 恢复为 global {g['volume']}%")
    else:
        msgs.append("手柄未连接，跳过音量恢复")

    if st:
        snap = st.get("snapshot") or {}
        want = snap.get("default_sink")
        if want and want != get_default_sink():
            try:
                set_default_sink(want)
                msgs.append(f"默认输出恢复为 {want}")
            except Exception as e:
                msgs.append(f"恢复默认输出失败：{e}")

    clear_state(dd)
    return "；".join(msgs)


def active_profile_key(dd, game_is_running):

    if game_is_running and profile_path(dd, APPID_MHW).exists():
        return APPID_MHW
    return "global"


def read_system_haptic_percent(_dd=None):
    """从系统读取当前手柄 RL/RR 音量（%），失败返回 None。"""
    sink = find_dualsense_sink()
    if not sink:
        return None
    vols = sink_volume_percent(sink)
    for ch in ("rear-left", "rear-right"):
        if ch in vols:
            try:
                return int(str(vols[ch]).rstrip("%"))
            except ValueError:
                pass
    return None


# ---------------------------------------------------------------- 修复流程

def move_game_streams_to(sink_name, log=print, channels=(2,)):
    """把当前游戏的指定声道音频流（默认 2ch）移动到目标 sink。"""
    if not sink_name:
        return False
    moved = False
    for st in game_streams():
        if st.get("_channels") not in channels:
            continue
        cur = sink_name_by_index(st.get("sink"))
        if cur == sink_name:
            continue
        pactl("move-sink-input", str(st["index"]), sink_name)
        log(f"   游戏音频流 #{st['index']}（{st.get('_channels')}ch）{cur} → {sink_name}")
        moved = True
    return moved


def hdmi_or_default_sink(name):
    """name 是 DualSense 时返回 HDMI sink，否则原样返回。"""
    ds5 = find_dualsense_sink()
    if ds5 and name == ds5["name"]:
        hdmi = find_hdmi_sink()
        return hdmi["name"] if hdmi else name
    return name


def target_output_sink(prof=None):
    """profile.audio_out = hdmi 时优先返回 HDMI sink，否则返回当前默认输出。"""
    if prof and str(prof.get("audio_out", "hdmi")).lower() == "hdmi":
        hdmi = find_hdmi_sink()
        if hdmi:
            return hdmi["name"]
    return get_default_sink()


def ensure_game_audio_on_hdmi(streams, log):
    """把当前游戏的 2ch 音频流挪到 HDMI。"""
    hdmi = find_hdmi_sink()
    if not hdmi:
        log("!! 未找到 HDMI sink，跳过音频路由")
        return False
    moved = False
    for s in streams:
        if s.get("_channels") == 2 and s.get("sink") != hdmi["index"]:
            pactl("move-sink-input", str(s["index"]), hdmi["name"])
            log(f"   游戏音频流 #{s['index']} → HDMI")
            moved = True
    return moved


def wait_for_game_stream(channels, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        found = find_stream(game_streams(), channels)
        if found:
            return found
        time.sleep(0.3)
    return None


def find_game_stream(streams, channels):
    return find_stream(streams, channels)


# 兼容旧名
def wait_for_mh_stream(channels, timeout):
    return wait_for_game_stream(channels, timeout)


def do_fix(dd, timeout=15, retries=3):
    """
    核心修复：确保荒野的 4 声道触觉流建立在手柄上，
    且游戏 2ch 音频流固定在 HDMI。返回 (ok, message)。
    """
    logs = []

    def log(msg):
        logs.append(msg)
        print(msg)

    log(f"== 检查 {GAME_NAME} 音频流 ...")
    two = wait_for_game_stream(2, timeout)
    if not two:
        return False, f"等不到 {GAME_NAME} 的 2ch 音频流（游戏可能还在启动界面）"
    log(f"   2ch 音频流 #{two['index']} → {sink_name_by_index(two['sink'])}")

    four = find_stream(game_streams(), 4)
    if four:
        log(f"   4ch 触觉流已存在（#{four['index']}），无需 nudge")
        ensure_game_audio_on_hdmi(game_streams(), log)
        return True, "已就绪（触觉流已存在）"

    ds5 = find_dualsense_sink()
    if not ds5:
        return False, "未找到 DualSense 手柄音频设备（请确认手柄已 USB 连接）"

    default_before = get_default_sink()
    log(f"   nudge：默认输出 {default_before} → {ds5['name']} → 切回")
    ok = False
    try:
        for attempt in range(1, retries + 1):
            set_default_sink(ds5["name"])
            four = wait_for_game_stream(4, 6)
            if four:
                log(f"   ✔ 触觉流已建立：#{four['index']}，4ch → {ds5['name']}")
                ok = True
                break
            log(f"   第 {attempt}/{retries} 次 nudge 未成功，重试 ...")
            time.sleep(0.8)
    finally:
        try:
            set_default_sink(default_before)
            audio_target = hdmi_or_default_sink(default_before)
            log(f"   已恢复默认输出：{default_before}，游戏音频 → {audio_target}")
            # 关键：把游戏音频流也挪回去（否则默认输出恢复了、声音还留在手柄上）
            move_game_streams_to(audio_target, log)
        except Exception as e:
            log(f"!! 恢复默认输出/音频流失败：{e}")

    if not ok:
        return False, "nudge 多次失败：游戏没有建立触觉流（可能需要换游戏版本/Proton）"

    ensure_game_audio_on_hdmi(game_streams(), log)
    return True, "修复完成"


def do_refresh(dd, timeout=20, retries=3):
    """强制 nudge 一次，尝试让游戏重建触觉流（不改变启用状态/档位/快照）。"""
    logs = []

    def log(msg):
        logs.append(msg)
        print(msg)

    overall = find_game_stream(game_streams(), 4)
    ds5 = find_dualsense_sink()
    if not ds5:
        return False, "未找到 DualSense 手柄音频设备"
    default_before = get_default_sink()
    log(f"刷新：默认输出 {default_before} → {ds5['name']} → 切回")
    for attempt in range(1, retries + 1):
        try:
            set_default_sink(ds5["name"])
            four = wait_for_game_stream(4, 6)
            if four:
                log(f"✔ 触觉流存在：#{four['index']}")
                return True, "触觉流已就绪"
            log(f"第 {attempt}/{retries} 次刷新未成功，重试 ...")
            time.sleep(0.8)
        finally:
            try:
                set_default_sink(default_before)
                move_game_streams_to(hdmi_or_default_sink(default_before), log)
            except Exception:
                pass
    if overall:
        return True, "触觉流已存在（本次 nudge 未重建）"
    return False, "刷新失败：游戏没有建立触觉流"


def do_apply(dd, profile_key=None, timeout=15):
    """按 profile 应用：音量 + 修复。供自动模式（插件 watcher）调用。"""
    running = bool(game_streams()) or game_running()
    key = profile_key or active_profile_key(dd, running)
    prof = load_profile(dd, key)
    print(f"== 应用 profile: {key} (vibration={prof['vibration_enabled']}, volume={prof['volume']}%, fix={prof['fix']})")

    if not prof.get("enabled", True):
        return False, f"profile {key} 已禁用"

    sink = find_dualsense_sink()
    if not sink:
        return False, "未找到 DualSense 手柄音频设备"

    # 第一次改动系统前，先把"进入游戏前"的状态记录下来
    st = snapshot_system(dd)
    if st:
        print(f"   已记录进入游戏前快照：{st['snapshot'].get('default_sink')} / {st['snapshot'].get('volume_raw')}")

    set_haptic_volume(sink, prof["volume"])
    print(f"   手柄 RL/RR 音量 → {prof['volume']}%")

    if prof.get("fix", False):
        ok, msg = do_fix(dd, timeout=timeout)
        # 无论成功失败，都把默认输出恢复到 profile 指定的输出（默认 HDMI）
        try:
            set_default_sink(target_output_sink(prof))
            move_game_streams_to(get_default_sink(), print, channels=(2,))
        except Exception as e:
            print(f"!! 恢复默认输出失败：{e}")
        return ok, msg
    return True, "音量已应用（未执行 fix）"


def do_restore(dd, profile_key=None):
    """退出游戏/关闭启用震动：恢复到"进入游戏前"的音频大小和默认输出。"""
    msgs = []
    st = load_state(dd)
    if st:
        msgs.append("恢复进入游戏前快照")
    msgs.append(restore_system(dd))
    try:
        prof = load_profile(dd, profile_key) if profile_key else {}
        target = hdmi_or_default_sink(get_default_sink())
        if prof.get("audio_out", "hdmi") != "hdmi":
            target = get_default_sink()
        elif target != get_default_sink():
            set_default_sink(target)
        move_game_streams_to(target, print, channels=(2,))
    except Exception as e:
        print(f"!! 恢复音频输出失败：{e}")
    return True, "；".join(msgs)


# ---------------------------------------------------------------- 命令

def ucm_status():
    present = [f for f in UCM_FILES if (UCM_DIR / f).exists()]
    if present:
        return f"⚠ UCM 配置存在（可能被系统更新还原）：{', '.join(present)}"
    return "UCM 配置已禁用（修复生效中）"


def status_payload(dd):
    running = game_running()
    sink = find_dualsense_sink()
    streams = [{"index": x["index"], "channels": x["_channels"],
                "sink": sink_name_by_index(x["sink"])} for x in mh_streams()]
    st = load_state(dd)
    return {
        "game_name": GAME_NAME,
        "game_running": running,
        "default_sink": get_default_sink(),
        "sink_name": sink["name"] if sink else None,
        "sink_volumes": sink_volume_percent(sink) if sink else None,
        "haptic_percent": read_system_haptic_percent(dd),
        "streams": streams,
        "profile_key": active_profile_key(dd, running),
        "snapshot": st.get("snapshot") if st else None,
        "ucm_files": [f for f in UCM_FILES if (UCM_DIR / f).exists()],
    }


def cmd_status(args):
    dd = data_dir(args)
    running = game_running()
    if getattr(args, "json", False):
        print(json.dumps(status_payload(dd), ensure_ascii=False))
        return 0
    print("== 状态")
    print(f"   荒野运行中：{'是' if running else '否'}")
    print(f"   默认输出：{get_default_sink()}")

    sink = find_dualsense_sink()
    if sink:
        vols = sink_volume_percent(sink)
        print(f"   手柄 sink：{sink['name']}")
        print(f"   手柄声道音量：{vols}")
    else:
        print("   手柄 sink：未找到（手柄未连接？）")

    st = mh_streams()
    if st:
        for s in st:
            print(f"   荒野流 #{s['index']}：{s['_channels']}ch → {sink_name_by_index(s['sink'])}")
    else:
        print("   荒野流：无")

    key = active_profile_key(dd, running)
    prof = load_profile(dd, key)
    print(f"   当前 profile：{key}  {json.dumps(prof, ensure_ascii=False)}")

    st = load_state(dd)
    if st:
        snap = st.get("snapshot") or {}
        print(f"   游戏前快照：默认输出 {snap.get('default_sink')}，音量 {snap.get('volume_raw')}")
        print("   （游戏退出时将恢复为该快照）")
    else:
        print("   游戏前快照：无")

    print(f"   {ucm_status()}")
    return 0


def cmd_fix(args):
    dd = data_dir(args)
    ok, msg = do_fix(dd, timeout=args.timeout)
    print(("✔ " if ok else "✘ ") + msg)
    return 0 if ok else 1


def cmd_apply(args):
    dd = data_dir(args)
    ok, msg = do_apply(dd, profile_key=args.profile, timeout=args.timeout)
    print(("✔ " if ok else "✘ ") + msg)
    return 0 if ok else 1


def cmd_refresh(args):
    dd = data_dir(args)
    ok, msg = do_refresh(dd, timeout=args.timeout)
    print(("✔ " if ok else "✘ ") + msg)
    return 0 if ok else 1


def cmd_restore(args):
    dd = data_dir(args)
    ok, msg = do_restore(dd, profile_key=getattr(args, "profile", None))
    print(("✔ " if ok else "✘ ") + msg)
    return 0 if ok else 1


def cmd_volume(args):
    dd = data_dir(args)
    sink = find_dualsense_sink()
    if not sink:
        print("✘ 未找到 DualSense 手柄音频设备")
        return 1
    if args.action == "read":
        print(f"系统当前音量：{sink_volume_percent(sink)}")
        pct = read_system_haptic_percent()
        print(f"RL/RR = {pct}%")
        return 0
    if args.action == "set":
        if args.value is None:
            print("✘ 需要音量值，例如: volume set 130")
            return 2
        set_haptic_volume(sink, args.value)
        key = args.profile or active_profile_key(dd, game_running())
        prof = load_profile(dd, key)
        prof["volume"] = args.value
        p = save_profile(dd, key, prof)
        print(f"✔ 已设置 RL/RR = {args.value}%，并写入 profile {key}（{p}）")
        return 0
    print(f"✘ 未知操作: {args.action}")
    return 2


def settings_path(dd):
    return dd / "settings.json"


def load_settings(dd):
    p = settings_path(dd)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"scope": "auto"}


def save_settings(dd, settings):
    settings_path(dd).write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def cmd_settings(args):
    dd = data_dir(args)
    st = load_settings(dd)
    if args.action == "get":
        print(json.dumps(st, ensure_ascii=False))
        return 0
    key = args.key
    val = args.value
    if val in ("true", "false"):
        st[key] = val == "true"
    else:
        try:
            st[key] = int(val)
        except ValueError:
            st[key] = val
    save_settings(dd, st)
    print(json.dumps(st, ensure_ascii=False))
    return 0


def cmd_profiles(args):
    dd = data_dir(args)
    files = sorted((dd / "profiles").glob("*.json"))
    keys = [f.stem for f in files] or ["global"]
    if "global" not in keys:
        keys.insert(0, "global")
    if getattr(args, "json", False):
        out = {k: load_profile(dd, k) for k in keys}
        print(json.dumps(out, ensure_ascii=False))
        return 0
    print(f"== profiles 目录：{dd / 'profiles'}")
    if not files:
        print("   （空，使用默认值）")
    for k in keys:
        print(f"   [{k}] {json.dumps(load_profile(dd, k), ensure_ascii=False)}")
    return 0


def cmd_profile_copy(args):
    dd = data_dir(args)
    src, dst = args.src, args.dst
    prof = load_profile(dd, src)
    p = save_profile(dd, dst, prof)
    print(f"✔ 已复制 profile {src} -> {dst}（{p}）")
    return 0


def cmd_profile_set(args):
    dd = data_dir(args)
    key, field, value = args.key, args.field, args.value
    if field not in PROFILE_DEFAULTS:
        print(f"✘ 未知字段: {field}（可用: {', '.join(PROFILE_DEFAULTS)}）")
        return 2
    prof = load_profile(dd, key)
    if field == "volume":
        prof["volume"] = max(0, min(int(value), 300))
    elif field in ("enabled", "vibration_enabled", "fix"):
        prof[field] = str(value).lower() in ("1", "true", "yes", "on")
    else:
        prof[field] = str(value)
    p = save_profile(dd, key, prof)
    print(f"✔ {key}.{field} = {prof[field]}（{p}）")
    return 0


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="MHWild_PS5 触觉/震动 核心引擎（阶段 1）")
    ap.add_argument("--data-dir", default=None, help="配置/日志目录")
    ap.add_argument("--game-name", default="Monster Hunter Wilds",
                    help="当前游戏名（用于匹配音频流）")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("fix")
    p.add_argument("--timeout", type=int, default=15)
    p.set_defaults(func=cmd_fix)
    p = sub.add_parser("apply")
    p.add_argument("--profile", default=None, help="指定 profile key（默认按当前游戏/global）")
    p.add_argument("--timeout", type=int, default=15)
    p.set_defaults(func=cmd_apply)
    p = sub.add_parser("restore")
    p.add_argument("--profile", default=None)
    p.set_defaults(func=cmd_restore)
    p = sub.add_parser("refresh")
    p.add_argument("--timeout", type=int, default=20)
    p.set_defaults(func=cmd_refresh)
    p = sub.add_parser("volume")
    p.add_argument("action", choices=["read", "set"])
    p.add_argument("value", nargs="?", type=int, default=None)
    p.add_argument("--profile", default=None)
    p.set_defaults(func=cmd_volume)
    p = sub.add_parser("profiles")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_profiles)
    p = sub.add_parser("settings")
    p.add_argument("action", choices=["get", "set"])
    p.add_argument("key", nargs="?", default=None)
    p.add_argument("value", nargs="?", default=None)
    p.set_defaults(func=cmd_settings)
    p = sub.add_parser("profile-copy")
    p.add_argument("src")
    p.add_argument("dst")
    p.set_defaults(func=cmd_profile_copy)
    p = sub.add_parser("profile-set")
    p.add_argument("key")
    p.add_argument("field")
    p.add_argument("value")
    p.set_defaults(func=cmd_profile_set)

    args = ap.parse_args()
    global GAME_NAME
    GAME_NAME = args.game_name or GAME_NAME
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
