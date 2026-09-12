"""Compatibility helpers and legacy advanced command dialogs."""
import re
import tkinter as tk
from tkinter import ttk
from typing import Any
from .protocol import COLOR_NAMES, build_a6_frame, build_c0_frame, build_da_frame, hex_bytes, zone_mask
from .controller import d8_slots_for_zones, d8_state_after_tx, d8_tx_commands, d8_update_slots, execute_persistent_d8_tx
from .transports import BaseTransport
COMMAND_STATES = {
    "熄灭": 0x00,
    "常亮": 0x01,
    "慢闪": 0x02,
    "中闪": 0x03,
    "快闪": 0x04,
    "Fade in": 0x05,
    "Fade out": 0x06,
    "保持": 0x0B,
}
D8_FUNCTIONS = {"常亮": 0, "慢闪": 1, "中闪": 2, "快闪": 3}
D8_DISABLED_STATES = {"保持", "Fade in", "Fade out"}
COLOR_OPTIONS = tuple((code, name) for code, name in COLOR_NAMES.items() if code != 0xAA)
PALETTE_HEX = {
    0x00: "#FF0000",
    0x01: "#00B51A",
    0x02: "#1878FF",
    0x03: "#FF007C",
    0x04: "#FFFFFF",
    0x05: "#FFD400",
    0x06: "#66CCFF",
    0x07: "#00D878",
    0x08: "#8A4DFF",
    0x09: "#FF6A00",
    0x0A: "#FF8AE0",
    0x0B: "#1B90FF",
    0x0C: "#FFF29A",
    0x0D: "#007B66",
    0x0E: "#FF5C5C",
    0x0F: "#F8FAFF",
}
def d8_rgb_from_hex(value: str) -> tuple[int, int, int]:
    """Convert a CSS-style RGB colour to the canonical 4-bit D8 values."""

    text = value.strip()
    if text.startswith("#"):
        text = text[1:]
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", text):
        raise ValueError("D8 颜色必须是 #RRGGBB")
    channels = (int(text[index : index + 2], 16) for index in range(0, 6, 2))
    return tuple(min(15, (channel + 8) // 17) for channel in channels)  # type: ignore[return-value]


def palette_hex(code: int) -> str:
    """Return the closest CSS colour for a documented 4-bit palette code."""

    try:
        return PALETTE_HEX[int(code)]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("颜色 code 必须为 0..15") from exc


def d8_tx_completion_text(state_changed: bool) -> str:
    """Describe bridge completion without implying a target-side ACK."""

    if not state_changed:
        return "TX 完成：6 帧 / 1 事务（与本地记录状态相同；棒端无 ACK）"
    return "TX 完成：6 帧 / 1 事务（棒端无 ACK）"



class LegacyControls:
    def _send_a6(self) -> None:
        self._start_air_tx((build_a6_frame(self.color_var.get()),))

    def _send_da(self) -> None:
        mask1, mask2 = zone_mask(self._selected_zones())
        self._start_air_tx((build_da_frame(mask1, mask2),))

    def open_c0_dialog(self) -> None:
        dialog = self._modal(self, "所有分区变色", "520x330")
        draft = list(self.c0_colors)
        zone_var = tk.StringVar(value="A")
        color_var = tk.IntVar(value=draft[0])
        preview_var = tk.StringVar()

        content = ttk.Frame(dialog, padding=14)
        content.pack(fill="both", expand=True)
        content.columnconfigure(1, weight=1)
        ttk.Label(content, text="命令预览：").grid(row=0, column=0, sticky="w")
        self._readonly_entry(content, preview_var, 52).grid(row=0, column=1, columnspan=4, sticky="ew")
        ttk.Label(content, text="组别：").grid(row=1, column=0, sticky="w", pady=(11, 4))
        zone_box = ttk.Combobox(content, textvariable=zone_var, values=list("ABCDEFGHIJ"), state="readonly", width=7)
        zone_box.grid(row=1, column=1, sticky="w", pady=(11, 4))
        palette = ttk.LabelFrame(content, text="颜色", padding=(8, 6))
        palette.grid(row=2, column=0, columnspan=5, sticky="ew", pady=(3, 9))
        for column in range(4):
            palette.columnconfigure(column, weight=1)
        for index, (code, name) in enumerate(COLOR_OPTIONS):
            ttk.Radiobutton(palette, text=name, variable=color_var, value=code).grid(
                row=index // 4, column=index % 4, sticky="w", padx=3, pady=2
            )

        def refresh() -> None:
            selected = ord(zone_var.get()) - ord("A")
            draft[selected] = color_var.get()
            preview_var.set(hex_bytes(build_c0_frame(draft)))

        def change_zone(*_: Any) -> None:
            color_var.set(draft[ord(zone_var.get()) - ord("A")])
            refresh()

        zone_var.trace_add("write", change_zone)
        color_var.trace_add("write", lambda *_: refresh())
        refresh()
        actions = ttk.Frame(content)
        actions.grid(row=3, column=0, columnspan=5, sticky="e")
        ttk.Button(actions, text="取消", command=dialog.destroy).grid(row=0, column=0, padx=(0, 6))

        def send() -> None:
            try:
                frame = build_c0_frame(draft)
            except Exception as exc:
                self._show_error("C0 参数无效", str(exc))
                return
            self.c0_colors = draft
            dialog.destroy()
            self._start_air_tx((frame,))

        ttk.Button(actions, text="发射", command=send).grid(row=0, column=1)

    def _execute_persistent_d8_tx(
        self,
        transport: BaseTransport,
        zones: tuple[str, ...],
        word: int,
        frequency_hz: int,
        power_dbm: int,
        gap_us: int,
    ) -> Any:
        return execute_persistent_d8_tx(
            self._state_store,
            transport,
            zones,
            word,
            frequency_hz,
            power_dbm,
            gap_us,
        )

