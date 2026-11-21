#!/usr/bin/env bash
# ============================================================
# PS5 手柄震动修复 —— 恢复脚本
# 作用：把改名为 .bak 的 DualSense UCM 配置文件恢复回原名，
#       回到系统原始状态（即 ALSA 官方默认配置）。
# 用法：bash "restore-dualsense-haptics.sh"   （会提示输入 sudo 密码）
# 说明：恢复后 DualSense 会重新出现 Speaker/Headphones/Haptic 等
#       细分 profile，"切到 HDMI 音频后震动失效"的问题可能回来。
# ============================================================
set -euo pipefail

UCM_DIR="/usr/share/alsa/ucm2/USB-Audio/Sony"
FILES=("DualSense-PS5.conf" "DualSense-PS5-HiFi.conf")

echo "==> 恢复原始 UCM 配置 ..."
restored=0
for f in "${FILES[@]}"; do
    src="$UCM_DIR/$f"
    bak="$UCM_DIR/$f.bak"
    if [ -f "$bak" ] && [ ! -f "$src" ]; then
        echo "    恢复: $f.bak -> $f"
        sudo mv -v "$bak" "$src" || { echo "!! 恢复失败：$bak"; exit 1; }
        restored=1
    elif [ -f "$src" ]; then
        echo "    已是原始状态: $f（跳过）"
    else
        echo "    未找到 $f 或 $f.bak，跳过"
    fi
done

if [ "$restored" -eq 0 ]; then
    echo "==> 无需恢复（可能已是原始状态，或备份不存在）。"
else
    echo "==> 重启用户音频服务 ..."
    systemctl --user restart wireplumber 2>/dev/null || echo "    (wireplumber 重启失败，可忽略)"
    systemctl --user restart pipewire 2>/dev/null || echo "    (pipewire 重启失败，可忽略)"
    echo
    echo "==> 已恢复系统原始配置。请拔插手柄。"
    echo "    注意：恢复后手柄会回到 DualSense 细分 profile，"
    echo "          之前修的'切 HDMI 后震动失效'问题可能重新出现。"
fi
