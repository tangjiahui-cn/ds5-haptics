#!/usr/bin/env bash
# ============================================================
# PS5 手柄震动修复（DualSense / DualSense Edge）
# 作用：把 DualSense 专用的 ALSA UCM 配置文件改名，
#       让 PipeWire 回退到"无 UCM"的通用枚举，
#       手柄固定暴露为 4 声道设备（Analog Surround 4.0），
#       Wine/Proton 游戏才能把触觉(震动)数据写进后 2 个声道。
# 用法：bash "fix-dualsense-haptics.sh"   （会提示输入 sudo 密码）
# 恢复：运行同目录的 restore-dualsense-haptics.sh
# ============================================================
set -euo pipefail

UCM_DIR="/usr/share/alsa/ucm2/USB-Audio/Sony"
FILES=("DualSense-PS5.conf" "DualSense-PS5-HiFi.conf")

echo "==> 检查当前状态 ..."
changed=0
for f in "${FILES[@]}"; do
    src="$UCM_DIR/$f"
    dst="$UCM_DIR/$f.bak"
    if [ -f "$src" ]; then
        echo "    改名: $f -> $f.bak"
        sudo mv -v "$src" "$dst" || { echo "!! 改名失败：$src"; exit 1; }
        changed=1
    elif [ -f "$dst" ]; then
        echo "    已处理过: $f.bak（跳过）"
    else
        echo "    未找到: $src（系统版本可能不同，请检查）"
    fi
done

if [ "$changed" -eq 0 ]; then
    echo "==> 已经是修复状态，无需重复操作。"
else
    echo "==> 重启用户音频服务，让 PipeWire 重新匹配 UCM ..."
    systemctl --user restart wireplumber 2>/dev/null || echo "    (wireplumber 重启失败，可忽略)"
    systemctl --user restart pipewire 2>/dev/null || echo "    (pipewire 重启失败，可忽略)"
    echo
    echo "==> 完成。请："
    echo "    1. 拔掉手柄重新插上（USB）"
    echo "    2. 在音频设置里确认手柄显示为 'Analog Surround 4.0'"
    echo "    3. 进游戏测试震动（先把输出切到 HDMI 再测）"
fi

echo
echo "注意：SteamOS 系统更新 / alsa-ucm-conf 包更新会恢复这两个文件，"
echo "      更新后重新运行本脚本即可。"
