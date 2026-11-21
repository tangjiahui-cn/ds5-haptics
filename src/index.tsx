import { useEffect, useRef, useState } from "react";
import { call, definePlugin } from "@decky/api";
import {
  ButtonItem,
  Field,
  PanelSection,
  PanelSectionRow,
  SliderField,
  ToggleField,
} from "@decky/ui";
import { PLUGIN_VERSION } from "./version";

type UiState = {
  status?: any;
  profiles?: Record<string, any>;
  games?: { appid: string; name: string; installdir?: string }[];
  current?: { appid: string; name: string } | null;
  phase?: "idle" | "waiting" | "switching" | "refreshing";
  wait_seconds?: number;
  wait_total?: number;
  active_key?: string;
  effective?: any;
  desired_volume?: number;
};

const shortSink = (name?: string | null) => {
  if (!name) return "-";
  const n = name.toLowerCase();
  if (n.includes("dualsense")) return "手柄";
  if (n.includes("hdmi")) return "HDMI";
  return name;
};

const Content = () => {
  const [ui, setUi] = useState<UiState>({});
  const [volume, setVolume] = useState(150);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const volumeRef = useRef(150);
  const editRef = useRef(0);
  const debounce = useRef(0);

  const refresh = async () => {
    try {
      const d = await call<[], UiState>("get_ui_state");
      setUi(d || {});
    } catch (e) {
      // 轮询失败忽略
    }
  };

  useEffect(() => {
    refresh();
    const t = window.setInterval(refresh, 2000);
    return () => window.clearInterval(t);
  }, []);

  const status = ui.status || {};
  const effective = ui.effective || {};
  const phase = ui.phase || "idle";
  const current = ui.current;
  const gameRunning = !!current;
  const applied = !!status.snapshot;
  const vibrationEnabled = !!effective.vibration_enabled;
  const strength = effective.volume ?? 100;
  const desiredVolume = ui.desired_volume ?? strength;

  const hapticStream = (status.streams || []).find(
    (s: any) => s.channels === 4,
  );
  const audioStream = (status.streams || []).find(
    (s: any) => s.channels === 2,
  );
  const sinkConnected = !!status.sink_name;

  const lockedAll = phase === "waiting" || phase === "refreshing";
  const switching = phase === "switching";
  const strengthEditable =
    !lockedAll && vibrationEnabled && (phase === "idle" || switching);
  const toggleLocked = lockedAll || switching;
  const refreshEnabled =
    !lockedAll && !switching && vibrationEnabled && gameRunning && sinkConnected;

  // 同步显示值（未在拖动时）
  useEffect(() => {
    if (Date.now() - editRef.current > 1500) {
      const v = switching ? desiredVolume : strength;
      volumeRef.current = v;
      setVolume(v);
    }
  }, [strength, desiredVolume, switching]);

  const notify = (label: string, r: any) => {
    if (r?.ok === false || r?.error) {
      setMessage(`${label}失败：${r?.error || ""}`);
    } else {
      setMessage(`${label}：${r?.output || "完成"}`);
    }
  };

  const wrap = async (label: string, fn: () => Promise<any>) => {
    if (busy) return;
    setBusy(true);
    try {
      const r = await fn();
      notify(label, r);
    } catch (e: any) {
      setMessage(`${label}异常：${e?.message || e}`);
    } finally {
      setBusy(false);
      refresh();
    }
  };

  const applyVolume = (value: number) => {
    volumeRef.current = value;
    setVolume(value);
    editRef.current = Date.now();
    if (debounce.current) window.clearTimeout(debounce.current);
    debounce.current = window.setTimeout(() => {
      call("set_volume", value).then((r: any) => {
        if (r?.ok === false || r?.error) setMessage(`设置强度失败：${r?.error || ""}`);
        else if (switching) setMessage(`已记录 ${value}%（切换结束后生效）`);
        else setMessage(`震动强度 ${value}%`);
        refresh();
      });
    }, 350);
  };

  const toggleVibration = (enabled: boolean) =>
    wrap(enabled ? "启用震动" : "关闭震动", () =>
      call("set_vibration_enabled", enabled),
    );

  const togglePerGame = (enabled: boolean) =>
    wrap("按游戏配置文件", () => call("set_per_game_enabled", enabled));

  const refreshVibration = () =>
    wrap("刷新震动", () => call("refresh_vibration"));

  const vibrationDesc = (() => {
    if (phase === "waiting")
      return `等待游戏进入游玩（${ui.wait_seconds ?? 0} 秒）`;
    if (phase === "switching") return "切换中…（强度可调，取最后值）";
    if (phase === "refreshing") return "刷新中…";
    if (!gameRunning) return "开启后进游戏自动启用";
    if (vibrationEnabled) return "已启用 · 改强度实时生效";
    return "开启后立即生效";
  })();

  return (
    <>
      <PanelSection>
        <PanelSectionRow>
          <Field label="震动强度">
            {applied ? `${strength}%` : `${strength}%（未启用）`}
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="触觉流">
            {hapticStream ? `成功（#${hapticStream.index}）` : "未建立"}
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="输出流">
            {gameRunning
              ? shortSink(audioStream?.sink || status.default_sink)
              : shortSink(status.default_sink)}
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="配置">
            {gameRunning && ui.active_key !== "global"
              ? current?.name || "全局"
              : "全局"}
          </Field>
        </PanelSectionRow>

        {gameRunning ? (
          <PanelSectionRow>
            <ToggleField
              label="按游戏配置文件"
              description="关闭后改用默认配置文件"
              checked={ui.active_key !== "global"}
              disabled={busy || toggleLocked}
              onChange={togglePerGame}
            />
          </PanelSectionRow>
        ) : null}

        <PanelSectionRow>
          <ToggleField
            label="启用震动"
            description={vibrationDesc}
            checked={vibrationEnabled}
            disabled={busy || toggleLocked || !sinkConnected}
            onChange={toggleVibration}
          />
        </PanelSectionRow>

        <PanelSectionRow>
          <SliderField
            label={switching ? "震动强度（切换中，取最后值）" : "震动强度"}
            value={volume}
            min={0}
            max={200}
            step={5}
            showValue
            disabled={busy || !strengthEditable}
            onChange={applyVolume}
          />
        </PanelSectionRow>

        {[100, 130, 150].map((v) => (
          <PanelSectionRow key={v}>
            <ButtonItem
              layout="below"
              disabled={busy || !strengthEditable}
              onClick={() => applyVolume(v)}
            >
              {v === 100 ? "默认 100" : v === 130 ? "推荐 130" : "真实 150"}
            </ButtonItem>
          </PanelSectionRow>
        ))}
      </PanelSection>

      <PanelSection title="高级">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy || !refreshEnabled}
            onClick={refreshVibration}
          >
            刷新震动
          </ButtonItem>
        </PanelSectionRow>
        {message ? (
          <PanelSectionRow>
            <Field label="状态">{message}</Field>
          </PanelSectionRow>
        ) : null}
        {(status.ucm_files || []).length > 0 ? (
          <PanelSectionRow>
            <Field label="⚠ 音频配置">
              UCM 配置被系统更新还原，请在桌面终端重新执行 DualSense UCM 修复脚本。
            </Field>
          </PanelSectionRow>
        ) : null}
      </PanelSection>

      <div
        style={{
          textAlign: "center",
          padding: "8px 0 12px",
          fontSize: 11,
          color: "#7f8c99",
          letterSpacing: "0.05em",
        }}
      >
        v{PLUGIN_VERSION}
      </div>
    </>
  );
};

export default definePlugin(() => ({
  name: "DS5-Haptics",
  title: <div>DS5-Haptics</div>,
  content: <Content />,
  icon: <div>🎮</div>,
}));
