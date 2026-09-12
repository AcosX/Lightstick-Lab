# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-control contributors
"""Compact Tk/ttk desktop interface for Lightstick Lab and the ESP32 bridge."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import __version__
from .engine import Engine
from .server.manager import ServerManager
from .model import LogicalUpdate, RadioSettings
from .controller import TX_POLL_MIN_TIMEOUT_SECONDS, tx_poll_timeout_seconds, wait_for_tx_result
from .discovery import is_target_ble_device
from .protocol import MAX_TX_REPEAT, encode_air_pulses, hex_bytes, tx_pulses_args
from ._legacy_ui import LegacyControls, palette_hex, d8_rgb_from_hex as rgb_from_hex
from ._legacy_ui import COLOR_OPTIONS
from .recording import ClientRecording
from .state import StateError, StateStore, default_state
from .transports import (
    BLE_DEVICE_NAME,
    BaseTransport,
    BleTransport,
    HttpTransport,
    SerialTransport,
    TransportError,
    TransportStatus,
    sorted_serial_ports,
)
from .workers import TaskRunner, WorkerEvent


NETWORK_POLL_TIMEOUT_SECONDS = 40.0
NETWORK_POLL_INTERVAL_SECONDS = 0.5
VALID_RECORD_THRESHOLD = 32

NO_VALID_RECORD_WARNING = "警告：截止目前未接收到有效数据。"


def network_status_is_connected(result: Any) -> bool:
    """Return true only after the bridge has a usable non-zero station IP."""

    if not isinstance(result, dict):
        return False
    wifi = result.get("wifi") if isinstance(result.get("wifi"), dict) else result
    if not isinstance(wifi, dict) or not bool(wifi.get("connected")):
        return False
    ip = str(wifi.get("ip") or result.get("ip") or "").strip()
    return bool(ip) and ip != "0.0.0.0"


def http_url_from_network_status(result: Any) -> str:
    """Turn a connected network-status result into the bridge HTTP URL."""

    if not network_status_is_connected(result):
        return ""
    assert isinstance(result, dict)
    wifi = result.get("wifi") if isinstance(result.get("wifi"), dict) else result
    assert isinstance(wifi, dict)
    ip = str(wifi.get("ip") or result.get("ip") or "").strip()
    return f"http://{ip}" if ip else ""


def wait_for_network_connection(
    transport: BaseTransport,
    timeout: float = NETWORK_POLL_TIMEOUT_SECONDS,
    poll_interval: float = NETWORK_POLL_INTERVAL_SECONDS,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Poll ``GET_NETWORK_STATUS`` until Wi-Fi is connected with a real IP."""

    if timeout <= 0:
        raise TransportError("Wi-Fi 状态等待超时必须大于 0")
    deadline = clock() + timeout
    last: Any = None
    while True:
        remaining = deadline - clock()
        if remaining < 0:
            raise TransportError("Wi-Fi 连接超时（仍未获得有效 IP）")
        last = transport.request(
            "GET_NETWORK_STATUS",
            {},
            timeout=min(8.0, max(0.1, remaining)),
        )
        if network_status_is_connected(last):
            if isinstance(last, dict):
                return last
            raise TransportError("GET_NETWORK_STATUS 返回格式无效")
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleep(min(max(0.01, poll_interval), remaining))
    raise TransportError(f"Wi-Fi 连接超时（最后状态: {last!r}）")


poll_network_until_connected = wait_for_network_connection


def select_preferred_ble_device(devices: list[dict[str, str]]) -> dict[str, str] | None:
    """Prefer the firmware's advertised device name after a BLE scan."""

    return next(
        (
            device
            for device in devices
            if device.get("address") and is_target_ble_device(device)
        ),
        None,
    )


def select_preferred_serial_port(ports: list[dict[str, str]]) -> dict[str, str] | None:
    """Prefer USB serial paths while excluding Bluetooth virtual ports."""

    usable = sorted_serial_ports(ports)
    return usable[0] if usable else None


def profile_tx_command(profile: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Choose the bridge TX command from a freshly-read board Profile."""

    name = str(profile.get("name") or "").strip()
    if not name:
        raise ValueError("Profile 必须包含 name")
    if str(profile.get("data_hex") or "").strip():
        return "TX_PACKET", {"profile": name}
    durations = profile.get("durations_us")
    if isinstance(durations, list) and durations:
        return "TX_PULSES", {"profile": name}
    raise ValueError("Profile 必须包含 data_hex 或 durations_us 才能发射")


def recording_has_valid_data(progress: Any) -> bool:
    """Return true only after the UI's conservative raw-edge threshold."""

    if not isinstance(progress, dict):
        return False
    try:
        return int(progress.get("records", 0)) >= VALID_RECORD_THRESHOLD
    except (TypeError, ValueError):
        return False


def recording_warning_text(valid_data_seen: bool) -> str:
    """Keep the no-data warning visible until a usable edge count is observed."""

    return "" if valid_data_seen else NO_VALID_RECORD_WARNING


def recover_recording_session(
    recorder: ClientRecording,
    report: Callable[[Any], None] | None = None,
) -> dict[str, Any]:
    """Stop a preserved client session and drain it through EOS."""

    stopped = recorder.stop()
    if isinstance(stopped, dict) and stopped.get("discarded"):
        return stopped
    drained = recorder.drain(report)
    return {"stop": stopped, **drained}


class LightstickApp(LegacyControls, tk.Tk):
    """The compact two-tab desktop demo."""

    def __init__(self, state_store: StateStore | None = None) -> None:
        super().__init__()
        self.title("Lightstick Lab")
        self.geometry("900x650")
        self.minsize(850, 650)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.runner = TaskRunner()
        self.transport: BaseTransport | None = None
        self.transport_status = TransportStatus("未选择", False, "未连接")
        self.recorder: ClientRecording | None = None
        self._record_valid_data_seen = False
        self.c0_colors = [0] * 10
        self._state_store = state_store or StateStore()
        startup_error = ''
        try:
            self._startup_state = self._state_store.load_unlocked()
        except StateError as exc:
            # Display defaults only. Never overwrite a damaged file or use
            # guessed protocol state for TX; Engine operations reload the store.
            self._startup_state = default_state()
            startup_error = f'状态文件读取失败，请修复 {self._state_store.path} 后重试：{exc}'
        self.engine = Engine(store=self._state_store, initial_state=self._startup_state)
        if startup_error:
            self.engine.warning = startup_error
        self.engine.start()
        self.server_manager = ServerManager(self.engine)
        self._busy_tasks: set[str] = set()
        self._task_callbacks: dict[str, tuple[str, Callable[[Any], None] | None, Callable[[str], None] | None]] = {}
        self._preview_valid = False
        self._profile_dialog: tk.Toplevel | None = None
        self._record_dialog: tk.Toplevel | None = None
        self._wifi_dialog: tk.Toplevel | None = None
        self._raw_dialog: tk.Toplevel | None = None
        self._build_style()
        self._build_header()
        self._build_tabs()
        if startup_error:
            self.air_activity_var.set(startup_error)
        self._worker_after_id = self.after(80, self._poll_worker_events)
        self._status_after_id = self.after(200, self._refresh_engine_status)

    def _stop_connector(self):
        self.connector_var.set('关闭')
        self._switch_connector()

    def _restore_connector_selection(self, _error=None):
        connector = self.server_manager.connector
        key = connector.id if connector and connector.status().get('active') else None
        self.connector_var.set(next(name for name, value in self._connector_names.items() if value == key))

    def _switch_connector(self):
        key = self._connector_names[self.connector_var.get()]
        try:
            config = self._state_store.load_unlocked()['connector_configs'].get(key, {})
            if key is not None:
                frequency, repeat, gap, power = self._tx_settings()
                self.engine.radio = RadioSettings(frequency, power, repeat, gap)
        except Exception as exc:
            self._restore_connector_selection()
            self._show_error('外部控制参数无效', str(exc))
            return
        if not self._submit('connector-switch', lambda: self.server_manager.switch(key, config), '外部控制已切换',
                            self._restore_connector_selection, self._restore_connector_selection):
            self._restore_connector_selection()
            self.external_status_var.set('正在切换，请等待当前操作完成')

    def _configure_connector(self):
        key = self._connector_names[self.connector_var.get()]
        if key is None:
            self._switch_connector()
            return
        plugin = self.server_manager.registry.plugins[key]
        try:
            saved = self._state_store.load_unlocked()['connector_configs'].get(key, {})
        except StateError as exc:
            self._restore_connector_selection()
            self._show_error('外部控制参数无效', str(exc))
            return
        config = {**plugin.default_config, **saved}
        dialog = self._modal(self, plugin.display_name, '420x240')
        entries = {}
        for index, (name, kind) in enumerate(plugin.config_schema.items()):
            ttk.Label(dialog, text=name).grid(row=index, column=0, padx=10, pady=8)
            entries[name] = tk.StringVar(value=str(config.get(name, '')))
            ttk.Entry(dialog, textvariable=entries[name]).grid(row=index, column=1)
        def start():
            try:
                settings = {name: int(var.get()) if plugin.config_schema[name] == 'integer' else var.get() for name,var in entries.items()}
                frequency, repeat, gap, power = self._tx_settings()
                radio = RadioSettings(frequency, power, repeat, gap)
            except Exception as exc:
                self._show_error('外部控制启动失败', str(exc))
                return
            def operation():
                self.engine.radio = radio
                self.server_manager.switch(key, settings)
            def done(_):
                self._restore_connector_selection()
                if dialog.winfo_exists():
                    dialog.destroy()
            if not self._submit('connector-switch', operation, '外部控制已启动', done, self._restore_connector_selection):
                self.external_status_var.set('正在切换，请等待当前操作完成')
        ttk.Button(dialog, text='启动', command=start).grid(row=len(entries), column=1, pady=10)

    def _refresh_engine_status(self):
        status = self.engine.status()
        tx = status['tx']
        if tx['error']:
            summary = '发射状态：' + tx['error']
        elif tx['running']:
            summary = '正在发射' + ('，已有最新状态等待发送' if tx['pending'] else '')
        elif tx['transmitted']:
            summary = 'Bridge 发射完成；无目标 ACK'
        else:
            summary = '准备就绪'
        # Keep manual operation results/errors until the scheduler state changes.
        signature = (tx['running'], tx['pending'], tx['transmitted'], tx['failed'], tx['error'])
        previous = getattr(self, '_last_tx_signature', None)
        if previous is not None and signature != previous and 'air-tx' not in self._busy_tasks:
            self.air_activity_var.set(summary)
        self._last_tx_signature = signature
        external = self.server_manager.status()
        active = '正在监听' if external.get('active') else '未启动'
        address = external.get('address')
        endpoint = f'{address[0]}:{address[1]}' if address else ''
        self.external_status_var.set(f"{active}  {endpoint}  {external.get('error','')}")
        self.external_bridge_var.set(f"Bridge：{'已连接' if status['connected'] else '未连接'}    协议：{self.engine.protocol.display_name}")
        self.external_tx_var.set(summary + f"    等待状态：{tx['pending']}" + (f"\n{self.engine.warning}" if self.engine.warning else ''))
        self.external_metrics_var.set(
            f"接收更新：{tx['received']}    去重：{tx['deduplicated']}    合并：{tx['coalesced']}\n"
            f"发射完成：{tx['transmitted']}    失败：{tx['failed']}    不支持：{tx['unsupported']}\n"
            f"网络收包：{external.get('received',0)}    无效包：{external.get('malformed',0)}    拒绝：{external.get('rejected',0)}")
        self._status_after_id = self.after(200, self._refresh_engine_status)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("aqua")
        except tk.TclError:
            style.theme_use("clam")
        style.configure("Header.TLabel", font=("SF Pro Display", 18, "bold"))
        style.configure("Muted.TLabel", foreground="#667085")
        style.configure("Warning.TLabel", foreground="#9a6a00")
        style.configure("Status.TLabel", foreground="#1f4f73")
        style.configure("TNotebook.Tab", padding=(20, 7))
        style.configure("Small.TButton", padding=(7, 3))

    def _build_header(self) -> None:
        header = ttk.Frame(self, padding=(16, 10, 16, 4))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="Lightstick Lab", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text=f"ESP32-S3 · {__version__}", style="Muted.TLabel").grid(
            row=0, column=1, sticky="e", padx=(8, 14)
        )
        self.header_status = ttk.Label(header, text="未连接", style="Status.TLabel")
        self.header_status.grid(row=0, column=2, sticky="e")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

    def _build_tabs(self) -> None:
        tabs = ttk.Notebook(self)
        tabs.grid(row=1, column=0, sticky="nsew", padx=12, pady=(2, 12))
        self.lightstick_tab = ttk.Frame(tabs, padding=10)
        self.esp_tab = ttk.Frame(tabs, padding=10)
        self.external_tab = ttk.Frame(tabs, padding=16)
        tabs.add(self.lightstick_tab, text="应援棒")
        tabs.add(self.esp_tab, text="ESP32")
        tabs.add(self.external_tab, text="外部控制")
        self._build_lightstick_tab()
        self._build_esp_tab()
        self._build_external_tab()

    @staticmethod
    def _readonly_entry(parent: ttk.Widget, variable: tk.Variable, width: int = 20) -> ttk.Entry:
        return ttk.Entry(parent, textvariable=variable, width=width, state="readonly")

    @staticmethod
    def _modal(parent: tk.Misc, title: str, geometry: str, resizable: bool = False) -> tk.Toplevel:
        dialog = tk.Toplevel(parent)
        dialog.title(title)
        dialog.geometry(geometry)
        dialog.transient(parent)
        dialog.resizable(resizable, resizable)
        dialog.grab_set()
        return dialog

    def _build_lightstick_tab(self) -> None:
        tab = self.lightstick_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(3, weight=1)

        self.preview_var = tk.StringVar(value="")
        preview = ttk.Frame(tab)
        preview.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        preview.columnconfigure(1, weight=1)
        ttk.Label(preview, text="命令预览：").grid(row=0, column=0, sticky="w")
        self._readonly_entry(preview, self.preview_var, 1).grid(row=0, column=1, sticky="ew")

        self._effect_labels = {'off':'熄灭','solid':'常亮','slow':'慢闪','medium':'中闪','fast':'快闪','hold':'保持','fade_in':'Fade in','fade_out':'Fade out'}
        effects = tuple(dict.fromkeys(effect for plugin in self.engine.registry.plugins.values() for effect in plugin.capabilities.effects))
        self._effect_ids = {label:effect for effect,label in self._effect_labels.items() if effect in effects}
        self._effect_ids.update({effect:effect for effect in effects if effect not in self._effect_labels})
        self._protocol_names = {plugin.display_name: key for key, plugin in self.engine.registry.plugins.items()}
        self.air_family_var = tk.StringVar(value=self.engine.protocol.display_name)
        family = ttk.Frame(tab)
        family.grid(row=1, column=0, sticky="ew", pady=(0, 5))
        ttk.Label(family, text='协议：').grid(row=0, column=0, sticky='w')
        ttk.Combobox(family, textvariable=self.air_family_var, values=tuple(self._protocol_names), state='readonly', width=18).grid(row=0, column=1)
        self.rgb_hex_var = tk.StringVar(value="#FF0000")
        self.rgb_hex_label = ttk.Label(family, text="RGB：")
        self.rgb_hex_entry = ttk.Entry(family, textvariable=self.rgb_hex_var, width=12)
        self.rgb_hex_label.grid(row=0, column=3, sticky="e", padx=(26, 4))
        self.rgb_hex_entry.grid(row=0, column=4, sticky="w")
        self.capabilities_var = tk.StringVar(value='')
        self.capabilities_label = ttk.Label(family, textvariable=self.capabilities_var, style='Muted.TLabel', wraplength=650)
        self.capabilities_label.grid(row=1, column=0, columnspan=5, sticky='w', pady=(4,0))

        zones = ttk.Frame(tab)
        zones.grid(row=2, column=0, sticky="ew", pady=(0, 7))
        ttk.Label(zones, text="组别：").grid(row=0, column=0, sticky="w")
        self.all_zones_var = tk.BooleanVar(value=False)
        self.zone_vars = {zone: tk.BooleanVar(value=index == 0) for index, zone in enumerate(dict.fromkeys(z for p in self.engine.registry.plugins.values() for z in p.capabilities.zones))}
        self.zone_buttons: list[ttk.Checkbutton] = []
        all_button = ttk.Checkbutton(zones, text="全选", variable=self.all_zones_var, command=self._toggle_all_zones)
        all_button.grid(row=0, column=1, sticky="w", padx=(0, 5))
        self.zone_buttons.append(all_button)
        for index, (zone, variable) in enumerate(self.zone_vars.items(), start=2):
            button = ttk.Checkbutton(zones, text=zone, variable=variable, command=self._zone_changed)
            button.grid(row=index // 18, column=index % 18, sticky="w", padx=1)
            self.zone_buttons.append(button)

        body = ttk.Frame(tab)
        body.grid(row=3, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1, minsize=310)
        body.columnconfigure(1, weight=1, minsize=390)

        functions = ttk.LabelFrame(body, text="功能", padding=(9, 7))
        functions.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        for column in range(2):
            functions.columnconfigure(column, weight=1)
        self.command_state_var = tk.StringVar(value="常亮")
        self.function_buttons: dict[str, ttk.Button] = {}
        states = tuple(self._effect_ids)
        for index, state in enumerate(states):
            button = ttk.Button(
                functions,
                text=state,
                style="Small.TButton",
                command=lambda selected=state: self._send_state(selected),
            )
            button.grid(row=index // 2, column=index % 2, sticky="ew", padx=3, pady=3)
            self.function_buttons[state] = button
        ttk.Separator(functions, orient="horizontal").grid(row=(len(states)+1)//2, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Button(
            functions,
            text="所有分区变色",
            style="Small.TButton",
            command=self.open_c0_dialog,
        ).grid(row=(len(states)+1)//2+1, column=0, columnspan=2, sticky="ew", padx=3, pady=3)
        ttk.Button(functions, text="脉冲", style="Small.TButton", command=self._send_a6).grid(
            row=(len(states)+1)//2+2, column=1, sticky="ew", padx=3, pady=3
        )
        ttk.Button(functions, text="解锁", style="Small.TButton", command=self._send_da).grid(
            row=(len(states)+1)//2+2, column=0, sticky="ew", padx=3, pady=3
        )

        colors = ttk.LabelFrame(body, text="颜色", padding=(9, 7))
        self.colors_frame = colors
        colors.grid(row=0, column=1, sticky="nsew")
        for column in range(4):
            colors.columnconfigure(column, weight=1)
        self.color_var = tk.IntVar(value=0)
        self.color_buttons: list[ttk.Radiobutton] = []
        for index, (code, name) in enumerate(COLOR_OPTIONS):
            button = ttk.Radiobutton(colors, text=name, variable=self.color_var, value=code)
            button.grid(row=index // 4, column=index % 4, sticky="w", padx=3, pady=2)
            self.color_buttons.append(button)

        settings = ttk.LabelFrame(tab, text="发射参数", padding=(9, 5))
        settings.grid(row=4, column=0, sticky="ew", pady=(7, 0))
        self.air_frequency_var = tk.StringVar(value="433920000")
        self.air_repeat_var = tk.StringVar(value="1")
        self.air_gap_var = tk.StringVar(value="20000")
        self.air_power_var = tk.IntVar(value=-20)
        ttk.Label(settings, text="频率 Hz").grid(row=0, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.air_frequency_var, width=12).grid(row=0, column=1, padx=(4, 13))
        ttk.Label(settings, text="重复").grid(row=0, column=2, sticky="w")
        ttk.Entry(settings, textvariable=self.air_repeat_var, width=4).grid(row=0, column=3, padx=(4, 13))
        ttk.Label(settings, text="间隔 us").grid(row=0, column=4, sticky="w")
        ttk.Entry(settings, textvariable=self.air_gap_var, width=8).grid(row=0, column=5, padx=(4, 14))
        ttk.Label(settings, text="−").grid(row=0, column=6, sticky="e")
        power = tk.Scale(
            settings,
            from_=-30,
            to=10,
            orient="horizontal",
            variable=self.air_power_var,
            showvalue=False,
            resolution=1,
            length=150,
            highlightthickness=0,
        )
        power.grid(row=0, column=7, sticky="ew", padx=2)
        ttk.Label(settings, text="+").grid(row=0, column=8, sticky="w")
        self.power_label = ttk.Label(settings, text="-20 dBm", width=8)
        self.power_label.grid(row=0, column=9, sticky="w", padx=(5, 0))
        self.air_activity_var = tk.StringVar(value="准备就绪")
        ttk.Label(tab, textvariable=self.air_activity_var, style="Muted.TLabel", wraplength=780).grid(
            row=5, column=0, sticky="w", pady=(5, 0)
        )

        self.air_family_var.trace_add("write", lambda *_: self._family_changed())
        self.color_var.trace_add("write", lambda *_: self._color_changed())
        self.rgb_hex_var.trace_add("write", lambda *_: self.refresh_preview())
        self.air_power_var.trace_add("write", lambda *_: self._update_power_label())
        self._family_changed()

    def _build_external_tab(self):
        tab = self.external_tab
        tab.columnconfigure(0, weight=1)
        connection = ttk.LabelFrame(tab, text='控制来源', padding=14)
        connection.grid(row=0, column=0, sticky='ew')
        connection.columnconfigure(1, weight=1)
        self._connector_names = {'关闭': None, **{p.display_name: key for key,p in self.server_manager.registry.plugins.items()}}
        saved = self._startup_state
        self.connector_var = tk.StringVar(value=next((name for name,key in self._connector_names.items() if key == saved['selected_connector']), '关闭'))
        ttk.Label(connection, text='外部控制').grid(row=0, column=0, padx=(0,12))
        selector = ttk.Combobox(connection, textvariable=self.connector_var, values=tuple(self._connector_names), state='readonly', width=22)
        selector.grid(row=0, column=1, sticky='w')
        selector.bind('<<ComboboxSelected>>', lambda event: self._switch_connector())
        ttk.Button(connection, text='启动 / 配置', command=self._configure_connector).grid(row=0, column=2, padx=8)
        ttk.Button(connection, text='停止', command=self._stop_connector).grid(row=0, column=3)
        self.external_status_var = tk.StringVar(value='未启动')
        ttk.Label(connection, textvariable=self.external_status_var, wraplength=700).grid(row=1, column=0, columnspan=4, sticky='w', pady=(12,0))
        bridge = ttk.LabelFrame(tab, text='发射状态', padding=14)
        bridge.grid(row=1, column=0, sticky='ew', pady=14)
        self.external_bridge_var = tk.StringVar(value='Bridge 未连接')
        ttk.Label(bridge, textvariable=self.external_bridge_var, wraplength=700).grid(row=0, column=0, sticky='w')
        self.external_tx_var = tk.StringVar(value='等待输入')
        ttk.Label(bridge, textvariable=self.external_tx_var, wraplength=700).grid(row=1, column=0, sticky='w', pady=(8,0))
        statistics = ttk.LabelFrame(tab, text='运行统计', padding=14)
        statistics.grid(row=2, column=0, sticky='ew')
        self.external_metrics_var = tk.StringVar(value='')
        ttk.Label(statistics, textvariable=self.external_metrics_var, justify='left', wraplength=700).grid(row=0, column=0, sticky='w')

    def _toggle_all_zones(self) -> None:
        selected = self.all_zones_var.get()
        active_zones = self.engine.protocol.capabilities.zones
        for zone, variable in self.zone_vars.items():
            variable.set(selected if zone in active_zones else False)
        self.refresh_preview()

    def _zone_changed(self) -> None:
        active_zones = self.engine.protocol.capabilities.zones
        self.all_zones_var.set(all(self.zone_vars[zone].get() for zone in active_zones))
        self.refresh_preview()

    def _family_changed(self):
        if getattr(self, '_reverting_protocol', False):
            return
        selected = self._protocol_names[self.air_family_var.get()]
        try:
            self.engine.select_protocol(selected, blocking=False)
        except Exception as exc:
            self.air_activity_var.set(str(exc))
            self._reverting_protocol = True
            self.air_family_var.set(self.engine.protocol.display_name)
            self._reverting_protocol = False
            return
        caps = self.engine.protocol.capabilities
        for zone, button in zip(self.zone_vars, self.zone_buttons[1:]):
            button.state(['!disabled'] if zone in caps.zones else ['disabled'])
            if zone not in caps.zones:
                self.zone_vars[zone].set(False)
        for widget in (self.rgb_hex_label, self.rgb_hex_entry):
            widget.grid() if caps.color_mode == 'rgb' else widget.grid_remove()
        effects = self._effect_ids
        for label, button in self.function_buttons.items():
            button.state(['!disabled'] if effects[label] in caps.effects else ['disabled'])
        unsupported = [label for label,effect in effects.items() if effect not in caps.effects]
        self.capabilities_var.set('当前协议不支持：' + '、'.join(unsupported) if unsupported else '')
        self.capabilities_label.grid() if unsupported else self.capabilities_label.grid_remove()
        if effects[self.command_state_var.get()] not in caps.effects:
            self.command_state_var.set(next(label for label,effect in effects.items() if effect in caps.effects))
        for button in self.color_buttons:
            button.destroy()
        options = tuple((code,name) for code,name,_ in caps.palette) or COLOR_OPTIONS
        if self.color_var.get() not in {code for code,_ in options}:
            self.color_var.set(options[0][0])
        self.color_buttons = []
        for index,(code,name) in enumerate(options):
            button = ttk.Radiobutton(self.colors_frame, text=name, variable=self.color_var, value=code)
            button.grid(row=index//4, column=index%4, sticky='w', padx=3, pady=2)
            self.color_buttons.append(button)
        self._zone_changed()

    def _color_changed(self):
        colors = {code:color for code,_,color in self.engine.protocol.capabilities.palette}
        self.rgb_hex_var.set(colors.get(self.color_var.get()) or palette_hex(self.color_var.get()))
        self.refresh_preview()

    def _update_power_label(self) -> None:
        self.power_label.configure(text=f"{self.air_power_var.get()} dBm")

    def _selected_zones(self) -> list[str]:
        return [zone for zone, variable in self.zone_vars.items() if variable.get()]

    def _current_update(self):
        effect = self._effect_ids[self.command_state_var.get()]
        caps = self.engine.protocol.capabilities
        palette = self.color_var.get() if caps.color_mode == 'palette' else None
        if palette is not None:
            colors = {code:color for code,_,color in caps.palette}
            rgb = rgb_from_hex(colors.get(palette) or palette_hex(palette))
        elif effect == 'off':
            rgb = (0, 0, 0)
        else:
            rgb = rgb_from_hex(self.rgb_hex_var.get())
        return LogicalUpdate(tuple(self._selected_zones()), rgb, effect, palette)

    def _current_frames(self):
        return self.engine.preview(self._current_update()).frames

    def refresh_preview(self) -> None:
        try:
            frames = self._current_frames()
        except Exception as exc:
            self._preview_valid = False
            self.preview_var.set(f"输入错误：{exc}")
            return
        self._preview_valid = True
        self.preview_var.set("  |  ".join(hex_bytes(frame) for frame in frames))

    def _send_state(self, state: str) -> None:
        self.command_state_var.set(state)
        self.refresh_preview()
        self._start_air_tx()

    def _tx_settings(self) -> tuple[int, int, int, int]:
        frequency = int(self.air_frequency_var.get())
        repeat = int(self.air_repeat_var.get())
        gap_us = int(self.air_gap_var.get())
        power = int(self.air_power_var.get())
        if not 1 <= repeat <= MAX_TX_REPEAT:
            raise ValueError(f"重复次数必须为 1..{MAX_TX_REPEAT}")
        if gap_us < 0:
            raise ValueError("间隔不能小于 0")
        if not -30 <= power <= 10:
            raise ValueError("功率必须为 -30..10 dBm")
        return frequency, repeat, gap_us, power

    def _start_air_tx(self, frames=None):
        if self._connected_transport('无法发射') is None:
            return
        if 'air-tx' in self._busy_tasks:
            self.air_activity_var.set('正在发射，请等待本次完成')
            return
        try:
            frequency, repeat, gap, power = self._tx_settings()
            radio = RadioSettings(frequency, power, repeat, gap)
            if frames is None:
                update = self._current_update()
                self.engine.preview(update, radio)
                operation = lambda: self.engine.execute_update(update, radio)
            else:
                commands = [('TX_PULSES', tx_pulses_args(encode_air_pulses(frame), frequency, power, repeat, gap)) for frame in frames]
                operation = lambda: self.engine.execute_commands(commands)
        except Exception as exc:
            self._show_error('发射参数无效', str(exc))
            return
        self.air_activity_var.set('正在发射并等待 TX 结果')
        self._submit('air-tx', operation, '发射完成',
                     lambda value: self.air_activity_var.set('Bridge 发射完成；无目标 ACK'),
                     lambda detail: self.air_activity_var.set('发射失败：' + detail))

    def _build_esp_tab(self) -> None:
        tab = self.esp_tab
        tab.columnconfigure(0, weight=1)

        status = ttk.LabelFrame(tab, text="状态", padding=(10, 7))
        status.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        status.columnconfigure(0, weight=1)
        self.esp_status_var = tk.StringVar(value="未连接")
        ttk.Label(status, textvariable=self.esp_status_var).grid(row=0, column=0, sticky="w")
        ttk.Button(status, text="刷新状态", style="Small.TButton", command=self.refresh_bridge_status).grid(
            row=0, column=1, padx=(8, 0)
        )
        ttk.Button(status, text="断开", style="Small.TButton", command=self.disconnect_transport).grid(
            row=0, column=2, padx=(6, 0)
        )

        transport = ttk.LabelFrame(tab, text="传输", padding=(10, 8))
        transport.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        transport.columnconfigure(1, weight=1)
        transport.columnconfigure(4, weight=1)
        self.serial_port_var = tk.StringVar(value="未扫描")
        self.ble_address_var = tk.StringVar(value="未扫描")
        self.http_url_var = tk.StringVar(value="")
        ttk.Button(transport, text="扫描串口", command=self.scan_serial_and_connect).grid(row=0, column=0, sticky="w")
        self._readonly_entry(transport, self.serial_port_var, 26).grid(row=0, column=1, sticky="ew", padx=(6, 13))
        ttk.Button(transport, text="扫描 BLE", command=self.scan_ble_and_connect).grid(row=0, column=2, sticky="w")
        self._readonly_entry(transport, self.ble_address_var, 26).grid(row=0, column=3, sticky="ew", padx=(6, 0))
        ttk.Label(transport, text="HTTP URL").grid(row=1, column=0, sticky="w", pady=(9, 0))
        self._readonly_entry(transport, self.http_url_var, 54).grid(row=1, column=1, columnspan=2, sticky="ew", padx=(6, 7), pady=(9, 0))
        ttk.Button(transport, text="使用 HTTP", command=self.connect_http).grid(row=1, column=3, sticky="e", pady=(9, 0))

        actions = ttk.LabelFrame(tab, text="操作", padding=(10, 9))
        actions.grid(row=2, column=0, sticky="ew")
        ttk.Button(actions, text="Wi-Fi 配网", command=self.open_wifi_dialog).grid(row=0, column=0, padx=(0, 7))
        ttk.Button(actions, text="板内 Profile", command=self.open_profile_dialog).grid(row=0, column=1, padx=7)
        ttk.Button(actions, text="客户端录制", command=self.open_record_dialog).grid(row=0, column=2, padx=7)
        ttk.Button(actions, text="原始命令", command=self.open_raw_dialog).grid(row=0, column=3, padx=7)
        self.esp_activity_var = tk.StringVar(value="")
        ttk.Label(tab, textvariable=self.esp_activity_var, style="Muted.TLabel").grid(
            row=3, column=0, sticky="w", pady=(10, 0)
        )

    def _connected_transport(self, title: str) -> BaseTransport | None:
        if self.transport is None or not self.transport_status.connected:
            self._show_error(title, "请先扫描并连接串口或 BLE，或使用 BLE 返回的 HTTP URL。")
            return None
        return self.transport

    def _execute_tx(self, transport: BaseTransport, commands: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
        return self.engine.execute_commands(commands)

    def _connect_owned_transport(self, transport):
        try:
            status = transport.connect()
            self.engine.attach(transport)
            self.engine.start()
            return status
        except Exception:
            transport.disconnect()
            raise

    def scan_serial_and_connect(self) -> None:
        def operation() -> dict[str, Any]:
            ports = SerialTransport.list_ports()
            selected = select_preferred_serial_port(ports)
            if selected is None:
                raise TransportError("未找到可用串口")
            port = str(selected.get("device") or "")
            transport = SerialTransport(port, 921600)
            return {"ports": ports, "transport": transport, "status": self._connect_owned_transport(transport)}

        self._submit("scan-serial", operation, "串口已连接", self._connected_from_scan)

    def scan_ble_and_connect(self) -> None:
        def operation() -> dict[str, Any]:
            devices = BleTransport.scan()
            device = select_preferred_ble_device(devices)
            if device is None:
                raise TransportError(f"未找到 {BLE_DEVICE_NAME}")
            address = str(device.get("address") or "")
            transport = BleTransport(address)
            status = self._connect_owned_transport(transport)
            try:
                network = transport.request("GET_NETWORK_STATUS", {}, timeout=8)
            except Exception as exc:
                network = {"warning": str(exc)}
            return {"devices": devices, "device": device, "transport": transport, "status": status, "network": network}

        self._submit("scan-ble", operation, "BLE 已连接", self._connected_from_scan)

    def _connected_from_scan(self, result: Any) -> None:
        if not isinstance(result, dict) or not isinstance(result.get("status"), TransportStatus):
            self._show_error("连接失败", "连接结果格式无效")
            return
        transport = result.get("transport")
        if not isinstance(transport, BaseTransport):
            self._show_error("连接失败", "传输实例无效")
            return
        self.transport = transport
        self._update_transport_status(result["status"])
        if result["status"].kind == "USB Serial":
            self.serial_port_var.set(str(result["status"].detail).split(" @ ")[0])
        if result["status"].kind == "BLE GATT":
            self.ble_address_var.set(str(result["status"].detail))
            url = http_url_from_network_status(result.get("network"))
            if url:
                self.http_url_var.set(url)
        self.refresh_bridge_status()

    @staticmethod
    def _best_effort_disconnect(transport: BaseTransport) -> None:
        try:
            transport.disconnect()
        except Exception:
            pass

    def connect_http(self) -> None:
        url = self.http_url_var.get().strip()
        if not url:
            self._show_error("HTTP 地址为空", "请先通过 BLE 获取已连接 Wi-Fi 的 IP。")
            return

        def operation() -> dict[str, Any]:
            transport = HttpTransport(url)
            return {"transport": transport, "status": self._connect_owned_transport(transport)}

        self._submit("connect-http", operation, "HTTP 已连接", self._connected_from_scan)

    def disconnect_transport(self) -> None:
        transport = self.transport
        if transport is None:
            return

        def finished(status: Any) -> None:
            self._update_transport_status(TransportStatus(self.transport_status.kind, False, "已断开"))
            self.transport = None

        self._submit("disconnect", self.engine.disconnect, "已断开", finished)

    def refresh_bridge_status(self) -> None:
        transport = self._connected_transport("无法刷新状态")
        if transport is None:
            return

        def result(value: Any) -> None:
            self._display_bridge_status(value)

        self._submit("bridge-status", lambda: transport.request("GET_STATUS", {}), "状态已刷新", result)

    def _display_bridge_status(self, value: Any) -> None:
        if not isinstance(value, dict):
            self.esp_status_var.set(str(value))
            return
        radio = value.get("radio") if isinstance(value.get("radio"), dict) else {}
        storage = value.get("storage") if isinstance(value.get("storage"), dict) else {}
        tx_busy = value.get("tx_busy", radio.get("tx_busy", False))
        recording = value.get("recording", storage.get("recording", False))
        self.esp_status_var.set(f"已连接 · TX {'忙' if tx_busy else '空闲'} · 录制 {'进行中' if recording else '停止'}")

    def open_wifi_dialog(self) -> None:
        dialog = self._modal(self, "Wi-Fi 配网", "430x205")
        self._wifi_dialog = dialog
        content = ttk.Frame(dialog, padding=14)
        content.pack(fill="both", expand=True)
        content.columnconfigure(1, weight=1)
        ssid_var = tk.StringVar()
        password_var = tk.StringVar()
        status_var = tk.StringVar()
        ttk.Label(content, text="SSID").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(content, textvariable=ssid_var, width=34).grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Label(content, text="密码").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(content, textvariable=password_var, show="*", width=34).grid(row=1, column=1, sticky="ew", pady=5)
        ttk.Label(content, textvariable=status_var, style="Muted.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 5)
        )

        def apply() -> None:
            transport = self._connected_transport("无法配网")
            if transport is None:
                return
            if not isinstance(transport, BleTransport):
                self._show_error("无法配网", "Wi-Fi 配网只能通过 BLE 连接执行。")
                return
            ssid = ssid_var.get().strip()
            password = password_var.get()
            if not ssid:
                self._show_error("Wi-Fi 参数无效", "请输入 SSID。")
                return
            status_var.set("正在保存并等待 IP")

            def operation() -> dict[str, Any]:
                transport.request("SET_WIFI_CONFIG", {"ssid": ssid, "password": password}, timeout=10)
                transport.request("CONNECT_WIFI", {}, timeout=10)
                return wait_for_network_connection(transport)

            def done(network: Any) -> None:
                url = http_url_from_network_status(network)
                self.http_url_var.set(url)
                self.esp_activity_var.set(f"Wi-Fi 已连接：{url}")
                if dialog.winfo_exists():
                    dialog.destroy()

            self._submit("wifi-config", operation, "Wi-Fi 已连接", done)

        actions = ttk.Frame(content)
        actions.grid(row=3, column=0, columnspan=2, sticky="e", pady=(4, 0))
        ttk.Button(actions, text="取消", command=dialog.destroy).grid(row=0, column=0, padx=(0, 7))
        ttk.Button(actions, text="应用", command=apply).grid(row=0, column=1)

    def open_profile_dialog(self) -> None:
        if self._connected_transport("无法管理 Profile") is None:
            return
        dialog = self._modal(self, "板内 Profile", "700x455", resizable=True)
        self._profile_dialog = dialog
        content = ttk.Frame(dialog, padding=12)
        content.pack(fill="both", expand=True)
        content.columnconfigure(1, weight=1)
        content.rowconfigure(0, weight=1)
        left = ttk.Frame(content)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        ttk.Label(left, text="板内 Profile").pack(anchor="w")
        self.profile_list = tk.Listbox(left, height=16, width=22, exportselection=False)
        self.profile_list.pack(fill="y", pady=(4, 6))
        self.profile_list.bind("<<ListboxSelect>>", lambda _event: self.load_profile())
        ttk.Button(left, text="刷新", command=self.refresh_profiles).pack(fill="x")
        right = ttk.Frame(content)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        ttk.Label(right, text="JSON").grid(row=0, column=0, sticky="w")
        self.profile_json = ScrolledText(right, height=18, wrap="none", font=("Menlo", 11))
        self.profile_json.grid(row=1, column=0, sticky="nsew", pady=(4, 8))
        self._set_profile_json(self._new_profile_template())
        controls = ttk.Frame(right)
        controls.grid(row=2, column=0, sticky="e")
        ttk.Button(controls, text="新建", command=lambda: self._set_profile_json(self._new_profile_template())).grid(
            row=0, column=0, padx=(0, 6)
        )
        ttk.Button(controls, text="读取", command=self.load_profile).grid(row=0, column=1, padx=6)
        ttk.Button(controls, text="保存", command=self.save_profile).grid(row=0, column=2, padx=6)
        ttk.Button(controls, text="删除", command=self.delete_profile).grid(row=0, column=3, padx=6)
        ttk.Button(controls, text="发射", command=self.send_selected_profile).grid(row=0, column=4, padx=6)
        ttk.Button(controls, text="关闭", command=dialog.destroy).grid(row=0, column=5, padx=(6, 0))
        self.refresh_profiles()

    def _profile_dialog_exists(self) -> bool:
        return self._profile_dialog is not None and bool(self._profile_dialog.winfo_exists())

    @staticmethod
    def _new_profile_template() -> dict[str, Any]:
        return {
            "name": "new-profile",
            "kind": "pulse",
            "frequency_hz": 433920000,
            "power_dbm": -20,
            "repeat": 1,
            "gap_us": 20000,
            "start_level": 1,
            "durations_us": [250, 500],
        }

    def _set_profile_json(self, profile: dict[str, Any]) -> None:
        self.profile_json.delete("1.0", "end")
        self.profile_json.insert("1.0", json.dumps(profile, ensure_ascii=False, indent=2))

    def refresh_profiles(self) -> None:
        transport = self._connected_transport("无法刷新 Profile")
        if transport is None:
            return

        def done(value: Any) -> None:
            if not self._profile_dialog_exists() or not isinstance(value, dict):
                return
            profiles = value.get("profiles", [])
            self.profile_list.delete(0, "end")
            for profile in profiles:
                if isinstance(profile, dict) and profile.get("name"):
                    self.profile_list.insert("end", str(profile["name"]))

        self._submit("profiles-list", lambda: transport.request("LIST_PROFILES", {}), "Profile 列表已刷新", done)

    def _selected_profile_name(self) -> str:
        if not self._profile_dialog_exists():
            return ""
        selection = self.profile_list.curselection()
        return self.profile_list.get(selection[0]).strip() if selection else ""

    def load_profile(self) -> None:
        transport = self._connected_transport("无法读取 Profile")
        name = self._selected_profile_name()
        if transport is None or not name:
            return

        def done(profile: Any) -> None:
            if not self._profile_dialog_exists() or not isinstance(profile, dict):
                return
            self._set_profile_json(profile)

        self._submit("profile-get", lambda: transport.request("GET_PROFILE", {"name": name}), "Profile 已读取", done)

    def _profile_json_value(self) -> dict[str, Any]:
        if not self._profile_dialog_exists():
            raise ValueError("Profile 窗口已关闭")
        try:
            value = json.loads(self.profile_json.get("1.0", "end").strip())
        except json.JSONDecodeError as exc:
            raise ValueError(f"Profile JSON 无效：{exc}") from exc
        if not isinstance(value, dict):
            raise ValueError("Profile 必须是 JSON object")
        if not str(value.get("name") or "").strip():
            raise ValueError("Profile JSON 必须包含 name")
        return value

    def save_profile(self) -> None:
        transport = self._connected_transport("无法保存 Profile")
        if transport is None:
            return
        try:
            profile = self._profile_json_value()
        except Exception as exc:
            self._show_error("保存 Profile 失败", str(exc))
            return

        def done(_: Any) -> None:
            self.refresh_profiles()

        self._submit("profile-set", lambda: transport.request("SET_PROFILE", {"profile": profile}), "Profile 已保存", done)

    def delete_profile(self) -> None:
        transport = self._connected_transport("无法删除 Profile")
        name = self._selected_profile_name()
        if transport is None or not name:
            self._show_error("删除 Profile 失败", "请先选择一个板内 Profile。")
            return
        self._submit(
            "profile-delete",
            lambda: transport.request("DELETE_PROFILE", {"name": name}),
            "Profile 已删除",
            lambda _: self.refresh_profiles(),
        )

    def send_selected_profile(self) -> None:
        transport = self._connected_transport("无法发射 Profile")
        name = self._selected_profile_name()
        if transport is None or not name:
            self._show_error("发射 Profile 失败", "请先选择一个板内 Profile。")
            return

        def operation() -> list[dict[str, Any]]:
            profile = transport.request("GET_PROFILE", {"name": name})
            if not isinstance(profile, dict):
                raise TransportError("GET_PROFILE 返回格式无效")
            command, args = profile_tx_command(profile)
            return self._execute_tx(transport, [(command, args)])

        self._submit("profile-tx", operation, "Profile 已发射")

    def open_raw_dialog(self) -> None:
        if self._connected_transport("无法发送原始命令") is None:
            return
        dialog = self._modal(self, "原始命令", "620x400", resizable=True)
        self._raw_dialog = dialog
        content = ttk.Frame(dialog, padding=12)
        content.pack(fill="both", expand=True)
        content.columnconfigure(1, weight=1)
        content.rowconfigure(1, weight=1)
        content.rowconfigure(3, weight=1)
        command_var = tk.StringVar(value="GET_STATUS")
        ttk.Label(content, text="命令").grid(row=0, column=0, sticky="w")
        ttk.Entry(content, textvariable=command_var, width=26).grid(row=0, column=1, sticky="ew", pady=(0, 6))
        ttk.Label(content, text="args JSON").grid(row=1, column=0, sticky="nw")
        args_box = ScrolledText(content, height=7, wrap="none", font=("Menlo", 11))
        args_box.grid(row=1, column=1, sticky="nsew", pady=(0, 8))
        args_box.insert("1.0", "{}")
        ttk.Label(content, text="响应").grid(row=2, column=0, sticky="nw")
        result_box = ScrolledText(content, height=7, wrap="none", font=("Menlo", 11), state="disabled")
        result_box.grid(row=3, column=1, sticky="nsew", pady=(0, 8))

        def send() -> None:
            transport = self._connected_transport("无法发送原始命令")
            if transport is None:
                return
            command = command_var.get().strip().upper()
            try:
                args = json.loads(args_box.get("1.0", "end").strip() or "{}")
            except json.JSONDecodeError as exc:
                self._show_error("原始命令参数无效", str(exc))
                return
            if not command or not isinstance(args, dict):
                self._show_error("原始命令参数无效", "命令不能为空，args 必须是 JSON object。")
                return

            def done(value: Any) -> None:
                if not dialog.winfo_exists():
                    return
                result_box.configure(state="normal")
                result_box.delete("1.0", "end")
                result_box.insert("1.0", json.dumps(value, ensure_ascii=False, indent=2))
                result_box.configure(state="disabled")

            self._submit("raw-command", lambda: transport.request(command, args), "原始命令完成", done)

        actions = ttk.Frame(content)
        actions.grid(row=4, column=0, columnspan=2, sticky="e")
        ttk.Button(actions, text="关闭", command=dialog.destroy).grid(row=0, column=0, padx=(0, 7))
        ttk.Button(actions, text="发送", command=send).grid(row=0, column=1)

    def open_record_dialog(self) -> None:
        if self._record_dialog is not None and self._record_dialog.winfo_exists():
            self._record_dialog.lift()
            return
        dialog = self._modal(self, "客户端录制", "640x405", resizable=True)
        self._record_dialog = dialog
        content = ttk.Frame(dialog, padding=12)
        content.pack(fill="both", expand=True)
        content.columnconfigure(1, weight=1)
        content.columnconfigure(3, weight=1)
        self.record_name_var = tk.StringVar(value="capture")
        self.record_path_var = tk.StringVar(value=str(Path.home() / "Desktop" / "capture.lsr"))
        self.record_profile_var = tk.StringVar(value="")
        self.record_frequency_var = tk.StringVar(value="433920000")
        self.record_bandwidth_var = tk.StringVar(value="203000")
        self.record_rate_var = tk.StringVar(value="4.8")
        self.record_modulation_var = tk.StringVar(value="2")
        self.record_min_edge_var = tk.StringVar(value="0")
        self.record_max_records_var = tk.StringVar(value="512")
        self.record_status_var = tk.StringVar(value="未开始")
        self.record_warning_var = tk.StringVar(value="")
        self._form_entry(content, "名称", self.record_name_var, 0, 0)
        self._form_entry(content, "Profile", self.record_profile_var, 0, 2)
        self._form_entry(content, "文件", self.record_path_var, 1, 0, columnspan=3, width=50)
        ttk.Button(content, text="选择", command=self.choose_record_path).grid(row=1, column=4, padx=(6, 0), pady=4)
        self._form_entry(content, "频率 Hz", self.record_frequency_var, 2, 0)
        self._form_entry(content, "带宽 Hz", self.record_bandwidth_var, 2, 2)
        self._form_entry(content, "速率 kbaud", self.record_rate_var, 3, 0)
        self._form_entry(content, "调制", self.record_modulation_var, 3, 2)
        self._form_entry(content, "最小边沿 us", self.record_min_edge_var, 4, 0)
        self._form_entry(content, "最大 records", self.record_max_records_var, 4, 2)
        ttk.Label(content, textvariable=self.record_status_var, style="Muted.TLabel").grid(
            row=5, column=0, columnspan=5, sticky="w", pady=(9, 2)
        )
        self.record_warning_label = ttk.Label(content, textvariable=self.record_warning_var, style="Warning.TLabel")
        self.record_warning_label.grid(row=6, column=0, columnspan=5, sticky="w", pady=(0, 8))
        actions = ttk.Frame(content)
        actions.grid(row=7, column=0, columnspan=5, sticky="e")
        ttk.Button(actions, text="关闭", command=dialog.destroy).grid(row=0, column=0, padx=(0, 7))
        ttk.Button(actions, text="停止并排空", command=self.stop_recording).grid(row=0, column=1, padx=(0, 7))
        ttk.Button(actions, text="开始", command=self.start_recording).grid(row=0, column=2)

    @staticmethod
    def _form_entry(
        parent: ttk.Widget,
        label: str,
        variable: tk.Variable,
        row: int,
        column: int,
        *,
        columnspan: int = 1,
        width: int = 18,
    ) -> ttk.Entry:
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", padx=(0, 5), pady=4)
        entry = ttk.Entry(parent, textvariable=variable, width=width)
        entry.grid(row=row, column=column + 1, columnspan=columnspan, sticky="ew", pady=4)
        return entry

    def choose_record_path(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self._record_dialog,
            title="选择 LSR1 输出文件",
            defaultextension=".lsr",
            filetypes=[("LSR1", "*.lsr"), ("All files", "*")],
        )
        if path:
            self.record_path_var.set(path)

    def _recording_args(self) -> tuple[ClientRecording, dict[str, Any]]:
        transport = self._connected_transport("无法开始录制")
        if transport is None:
            raise ValueError("未连接")
        name = self.record_name_var.get().strip()
        if not name:
            raise ValueError("请输入录制名称")
        max_records = int(self.record_max_records_var.get())
        if not 1 <= max_records <= 512:
            raise ValueError("最大 records 必须为 1..512")
        min_edge = int(self.record_min_edge_var.get())
        if not 0 <= min_edge <= 1_000_000:
            raise ValueError("最小边沿必须为 0..1000000 us")
        receiver = {
            "frequency_hz": int(self.record_frequency_var.get()),
            "rx_bandwidth_hz": int(self.record_bandwidth_var.get()),
            "data_rate_kbaud": float(self.record_rate_var.get()),
            "modulation": int(self.record_modulation_var.get()),
        }
        recording = {
            "edge_mode": "both",
            "min_edge_interval_us": min_edge,
            "client_chunk_records": max_records,
        }
        args: dict[str, Any] = {"receiver": receiver, "recording": recording}
        profile = self.record_profile_var.get().strip()
        if profile:
            args["profile"] = profile
        raw_path = self.record_path_var.get().strip()
        if not raw_path:
            raise ValueError("请选择输出文件")
        path = Path(os.path.expanduser(raw_path))
        return ClientRecording(transport, path, name, max_records), args

    def start_recording(self) -> None:
        if self.recorder is not None or "recording" in self._busy_tasks:
            self._show_error("无法开始录制", "已有客户端录制会话，请先停止并排空。")
            return
        try:
            recorder, args = self._recording_args()
        except Exception as exc:
            self._show_error("录制参数无效", str(exc))
            return
        self.recorder = recorder
        self._record_valid_data_seen = False
        self.record_status_var.set("正在开始")
        self.record_warning_var.set(recording_warning_text(self._record_valid_data_seen))

        def operation(report: Callable[[Any], None]) -> dict[str, Any]:
            recorder.start(args)
            return recorder.drain(report)

        self._submit_progress("recording", operation, "录制完成")

    def stop_recording(self) -> None:
        recorder = self.recorder
        if recorder is None:
            self._show_error("无法停止录制", "当前没有客户端录制会话。")
            return
        if "recording" in self._busy_tasks:
            self.record_status_var.set("已请求停止，正在排空至 EOS")
            self._submit("record-stop", recorder.stop, "已请求停止")
            return

        def recover(report: Callable[[Any], None]) -> dict[str, Any]:
            return recover_recording_session(recorder, report)

        self.record_status_var.set("正在恢复并排空至 EOS")
        self._submit_progress("record-recovery", recover, "录制已恢复并排空")

    def _submit(
        self,
        task: str,
        operation: Callable[[], Any],
        message: str,
        callback: Callable[[Any], None] | None = None,
        error_callback: Callable[[str], None] | None = None,
    ) -> bool:
        if task in self._busy_tasks:
            return False
        self._busy_tasks.add(task)
        self._task_callbacks[task] = (message, callback, error_callback)
        self.runner.submit(task, operation)
        return True

    def _submit_progress(
        self,
        task: str,
        operation: Callable[[Callable[[Any], None]], Any],
        message: str,
    ) -> bool:
        if task in self._busy_tasks:
            return False
        self._busy_tasks.add(task)
        self._task_callbacks[task] = (message, None, None)
        self.runner.submit_progress(task, operation)
        return True

    def _poll_worker_events(self) -> None:
        while True:
            try:
                event = self.runner.events.get_nowait()
            except Exception:
                break
            if event.kind == "progress":
                self._handle_progress(event)
                continue
            self._busy_tasks.discard(event.task)
            message, callback, error_callback = self._task_callbacks.pop(event.task, ("", None, None))
            if event.kind == "error":
                detail = (event.error or "操作失败").splitlines()[0]
                self.esp_activity_var.set(f"失败：{detail}")
                if event.task in {"recording", "record-recovery"}:
                    self.record_status_var.set("录制失败；可再次点击停止并排空。")
                    if self.recorder is not None and self.recorder.session_id is None:
                        self.recorder = None
                if error_callback:
                    error_callback(detail)
                self._show_error("操作失败", detail)
                continue
            if event.task in {"recording", "record-recovery"}:
                self._finish_recording(event.value)
            if callback:
                try:
                    callback(event.value)
                except Exception as exc:
                    self._show_error('操作结果处理失败', str(exc))
            if message:
                self.esp_activity_var.set(message)
        self._worker_after_id = self.after(80, self._poll_worker_events)

    def _handle_progress(self, event: WorkerEvent) -> None:
        if event.task not in {"recording", "record-recovery"} or not isinstance(event.value, dict):
            return
        value = event.value
        records = int(value.get("records", 0) or 0)
        self.record_status_var.set(
            f"录制中：{records} records · {value.get('bytes', 0)} bytes · EOS={value.get('eos', False)}"
        )
        if recording_has_valid_data(value):
            self._record_valid_data_seen = True
            self.record_warning_var.set("")

    def _finish_recording(self, value: Any) -> None:
        if isinstance(value, dict):
            self.record_status_var.set(
                f"录制完成：{value.get('records', 0)} records · {value.get('bytes', 0)} bytes"
            )
        self.record_warning_var.set(recording_warning_text(self._record_valid_data_seen))
        self.recorder = None

    def _update_transport_status(self, status: TransportStatus) -> None:
        self.transport_status = status
        marker = "已连接" if status.connected else "未连接"
        self.header_status.configure(text=f"{marker} · {status.kind}")
        if not status.connected:
            self.esp_status_var.set("未连接")

    def _show_error(self, title: str, detail: str) -> None:
        try:
            messagebox.showerror(title, detail, parent=self)
        except tk.TclError:
            pass

    def _close(self):
        for name in ('_worker_after_id', '_status_after_id'):
            timer = getattr(self, name, None)
            if timer is not None:
                self.after_cancel(timer)
                setattr(self, name, None)
        self.server_manager.close()
        self.engine.stop()
        self.transport = None
        self.destroy()


def run() -> None:
    LightstickApp().mainloop()

# Historical import compatibility; not used by the active controls.
def __getattr__(name):
    from . import _legacy_ui
    if hasattr(_legacy_ui, name):
        return getattr(_legacy_ui, name)
    raise AttributeError(name)
