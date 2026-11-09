# -*- coding: utf-8 -*-
"""杭电图书馆自动预约 —— 图形界面入口。

运行：python app.py
打包：build.bat（PyInstaller，生成 dist/HDU_Lib_AutoBook.exe）
"""
import json
import os
import re
import sys
import tempfile
import threading
import webbrowser
from datetime import datetime
from tkinter import messagebox

import customtkinter as ctk

from core import paths, updater
from core.booker import (DEFAULT_SETTINGS, WEEKDAYS, SeatBooker, expand_ranges,
                         load_rooms, next_schedule_time, room_of_seat, seat_number,
                         target_date, weekday_name)

APP_NAME = "杭电图书馆自动预约"
VERSION = "1.2.0"


def parse_seat_text(text):
    """把「001, 005-010」这类真实座位号文本解析成列表（去重保序）。"""
    seats = []
    for token in re.split(r"[,，;；、\s]+", (text or "").strip()):
        if not token:
            continue
        m = re.fullmatch(r"(\d+)\s*[-~到至]\s*(\d+)", token)
        if m:
            a, b = sorted((int(m.group(1)), int(m.group(2))))
            seats.extend(range(a, b + 1))
        elif token.isdigit():
            seats.append(int(token))
    seen, out = set(), []
    for s in seats:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def as_int(value, fallback):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


class AutoBookApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")
        self.title("{}  v{}".format(APP_NAME, VERSION))
        self.geometry("1000x700")
        self.minsize(900, 640)
        try:
            self.iconbitmap(paths.resource_path(os.path.join("assets", "icon.ico")))
        except Exception:
            pass

        self.rooms = load_rooms()
        self.saved = self._load_settings()
        self.worker = None
        self.booker = None
        self.stop_event = None
        self._checking_update = False
        self._downloading = False

        self._build_ui()
        self._apply_saved()
        self._refresh_preview()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # 启动后静默检查一次更新，失败不打扰用户
        self.after(1800, lambda: self._check_update(silent=True))

    # ---------- 配置读写 ----------
    @staticmethod
    def _load_settings():
        try:
            with open(paths.settings_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_settings(self, st):
        payload = dict(st)
        if not payload.get("remember_password"):
            payload.pop("password", None)
        try:
            with open(paths.settings_path(), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ---------- 界面 ----------
    def _card(self, parent, title):
        card = ctk.CTkFrame(parent, corner_radius=10)
        card.pack(fill="x", padx=6, pady=(0, 10))
        ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=14, weight="bold"),
                     anchor="w").pack(fill="x", padx=16, pady=(12, 6))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=(0, 14))
        return inner

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 6))
        ctk.CTkLabel(header, text=APP_NAME, font=ctk.CTkFont(size=22, weight="bold"),
                     anchor="w").pack(side="left")
        ctk.CTkLabel(header, text="  HDU Library Auto Booking", text_color="gray50",
                     font=ctk.CTkFont(size=13), anchor="w").pack(side="left", pady=(6, 0))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=12, pady=4)
        body.grid_columnconfigure(0, weight=0, minsize=450)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        form = ctk.CTkScrollableFrame(body, width=430, corner_radius=0, fg_color="transparent")
        form.grid(row=0, column=0, sticky="nsew")

        self._build_account_card(form)
        self._build_plan_card(form)
        self._build_seat_card(form)
        self._build_schedule_card(form)
        self._build_advanced_card(form)

        log_wrap = ctk.CTkFrame(body, corner_radius=10)
        log_wrap.grid(row=0, column=1, sticky="nsew", padx=(6, 6))
        log_wrap.grid_columnconfigure(0, weight=1)
        log_wrap.grid_rowconfigure(1, weight=1)

        log_head = ctk.CTkFrame(log_wrap, fg_color="transparent")
        log_head.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))
        ctk.CTkLabel(log_head, text="运行日志", font=ctk.CTkFont(size=14, weight="bold"),
                     anchor="w").pack(side="left")
        ctk.CTkButton(log_head, text="清空", width=56, height=26, fg_color="transparent",
                      border_width=1, text_color=("gray30", "gray80"),
                      hover_color=("gray85", "gray25"),
                      command=self._clear_log).pack(side="right")

        self.log_box = ctk.CTkTextbox(log_wrap, wrap="word", corner_radius=8,
                                      font=ctk.CTkFont(family="Consolas", size=12))
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.log_box.configure(state="disabled")

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=18, pady=(4, 14))
        footer.grid_columnconfigure(0, weight=1)

        self.status_label = ctk.CTkLabel(footer, text="● 就绪", anchor="w",
                                         font=ctk.CTkFont(size=13))
        self.status_label.grid(row=0, column=0, sticky="w")

        ctk.CTkButton(footer, text="数据目录", width=90, height=34, fg_color="transparent",
                      border_width=1, text_color=("gray30", "gray80"),
                      hover_color=("gray85", "gray25"),
                      command=self._open_data_dir).grid(row=0, column=1, padx=6)
        self.stop_btn = ctk.CTkButton(footer, text="停止", width=96, height=34,
                                      fg_color="#b4443c", hover_color="#93362f",
                                      state="disabled", command=self._stop)
        self.stop_btn.grid(row=0, column=2, padx=6)
        self.start_btn = ctk.CTkButton(footer, text="开始预约", width=130, height=34,
                                       font=ctk.CTkFont(size=14, weight="bold"),
                                       command=self._start)
        self.start_btn.grid(row=0, column=3, padx=(6, 0))

        self.update_btn = ctk.CTkButton(footer, text="检查更新", width=96, height=34,
                                        fg_color="transparent", border_width=1,
                                        text_color=("gray30", "gray80"),
                                        hover_color=("gray85", "gray25"),
                                        command=lambda: self._check_update(silent=False))
        self.update_btn.grid(row=0, column=4, padx=(6, 0))

    def _build_account_card(self, parent):
        inner = self._card(parent, "账号")
        inner.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(inner, text="学号", anchor="w").grid(row=0, column=0, sticky="w", pady=5)
        self.id_entry = ctk.CTkEntry(inner, placeholder_text="请输入学号")
        self.id_entry.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=5)

        ctk.CTkLabel(inner, text="密码", anchor="w").grid(row=1, column=0, sticky="w", pady=5)
        self.pwd_entry = ctk.CTkEntry(inner, placeholder_text="请输入密码", show="*")
        self.pwd_entry.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=5)

        row = ctk.CTkFrame(inner, fg_color="transparent")
        row.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.remember_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(row, text="记住密码", variable=self.remember_var).pack(side="left")
        ctk.CTkLabel(row, text="明文保存在本机配置文件", text_color="gray50",
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=8)

    def _build_plan_card(self, parent):
        inner = self._card(parent, "预约计划")
        inner.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(inner, text="目标日期", anchor="w").grid(row=0, column=0, sticky="w", pady=5)
        self.date_seg = ctk.CTkSegmentedButton(inner, values=["明天", "后天"],
                                               command=lambda _=None: self._refresh_preview())
        self.date_seg.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=5)
        self.date_seg.set("明天")

        ctk.CTkLabel(inner, text="时段", anchor="w").grid(row=1, column=0, sticky="w", pady=5)
        slot = ctk.CTkFrame(inner, fg_color="transparent")
        slot.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=5)
        slot.grid_columnconfigure(2, weight=1)

        hours = ["{:02d}:00".format(h) for h in range(6, 24)]
        self.start_hour = ctk.CTkOptionMenu(slot, values=hours, width=90,
                                            command=lambda _=None: self._refresh_preview())
        self.start_hour.pack(side="left")
        self.start_hour.set("10:00")
        ctk.CTkLabel(slot, text="至", width=24).pack(side="left", padx=6)
        self.end_hour = ctk.CTkOptionMenu(slot, values=hours, width=90,
                                          command=lambda _=None: self._refresh_preview())
        self.end_hour.pack(side="left")
        self.end_hour.set("22:00")

        self.preview_label = ctk.CTkLabel(inner, text="", anchor="w", text_color="gray55",
                                          font=ctk.CTkFont(size=12))
        self.preview_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

    def _build_seat_card(self, parent):
        inner = self._card(parent, "座位")
        inner.grid_columnconfigure(1, weight=1)

        self.mode_seg = ctk.CTkSegmentedButton(inner, values=["楼层整层", "自定义座位"],
                                               command=lambda _=None: self._toggle_seat_mode())
        self.mode_seg.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.mode_seg.set("楼层整层")

        self.room_label = ctk.CTkLabel(inner, text="楼层", anchor="w")
        self.room_label.grid(row=1, column=0, sticky="w", pady=5)
        room_names = list(self.rooms.keys())
        self.room_menu = ctk.CTkOptionMenu(inner, values=room_names or ["（无座位表）"],
                                           command=lambda _=None: self._refresh_seat_hint())
        self.room_menu.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=5)
        if room_names:
            self.room_menu.set(room_names[0])

        self.tier_rows = []
        hints = ("座位图上印的 3 位号码，如 220", "首选没抢到时再试，可留空", "仍然没抢到时的兜底，可留空")
        for i, (name, hint) in enumerate(zip(("首选", "次选", "备用"), hints), start=2):
            label = ctk.CTkLabel(inner, text=name, anchor="w")
            entry = ctk.CTkEntry(inner, placeholder_text=hint)
            self.tier_rows.append((label, entry))

        self.custom_hint = ctk.CTkLabel(inner, text="按顺序尝试，支持逗号分隔与 a-b 区间",
                                        anchor="w", text_color="gray50",
                                        font=ctk.CTkFont(size=11))

        self.extend_var = ctk.BooleanVar(value=True)
        self.extend_box = ctk.CTkCheckBox(inner, text="所选座位被占后，自动扩展到同楼层其它座位",
                                          variable=self.extend_var)
        self.extend_box.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))

        self._toggle_seat_mode()

    def _build_schedule_card(self, parent):
        inner = self._card(parent, "定时抢座（像闹钟）")
        inner.grid_columnconfigure(1, weight=1)

        self.sched_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(inner, text="启用定时：到点自动开始抢座", variable=self.sched_var,
                        command=self._refresh_schedule_preview).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        ctk.CTkLabel(inner, text="抢座时刻", anchor="w").grid(row=1, column=0, sticky="w", pady=4)
        self.sched_time = ctk.CTkEntry(inner, placeholder_text="19:55:00")
        self.sched_time.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=4)
        self.sched_time.insert(0, DEFAULT_SETTINGS["schedule_time"])
        self.sched_time.bind("<KeyRelease>", lambda _e: self._refresh_schedule_preview())

        ctk.CTkLabel(inner, text="每周时段", anchor="w").grid(row=2, column=0, sticky="w",
                                                              pady=(8, 2))
        hours = ["{:02d}:00".format(h) for h in range(6, 24)]
        self.day_rows = []
        for i, name in enumerate(WEEKDAYS):
            row = ctk.CTkFrame(inner, fg_color="transparent")
            row.grid(row=3 + i, column=0, columnspan=2, sticky="ew", pady=1)
            var = ctk.BooleanVar(value=False)
            ctk.CTkCheckBox(row, text=name, width=64, checkbox_width=18, checkbox_height=18,
                            variable=var,
                            command=self._refresh_schedule_preview).pack(side="left")
            start = ctk.CTkOptionMenu(row, values=hours, width=84,
                                      command=lambda _=None: self._refresh_schedule_preview())
            start.pack(side="left", padx=(8, 0))
            start.set("{:02d}:00".format(DEFAULT_SETTINGS["start_hour"]))
            ctk.CTkLabel(row, text="至", width=20).pack(side="left", padx=4)
            end = ctk.CTkOptionMenu(row, values=hours, width=84,
                                    command=lambda _=None: self._refresh_schedule_preview())
            end.pack(side="left")
            end.set("{:02d}:00".format(DEFAULT_SETTINGS["end_hour"]))
            self.day_rows.append({"var": var, "start": start, "end": end})

        self.sched_hint = ctk.CTkLabel(inner, text="", anchor="w", text_color="gray55",
                                       font=ctk.CTkFont(size=11), justify="left")
        self.sched_hint.grid(row=10, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self._refresh_schedule_preview()

    def _build_advanced_card(self, parent):
        self.advanced_open = False
        card = ctk.CTkFrame(parent, corner_radius=10)
        card.pack(fill="x", padx=6, pady=(0, 10))

        self.adv_toggle = ctk.CTkButton(card, text="高级设置  ▸", anchor="w", height=34,
                                        fg_color="transparent", text_color=("gray25", "gray85"),
                                        hover_color=("gray88", "gray22"),
                                        font=ctk.CTkFont(size=14, weight="bold"),
                                        command=self._toggle_advanced)
        self.adv_toggle.pack(fill="x", padx=8, pady=6)

        self.adv_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.adv_frame.grid_columnconfigure(1, weight=1)

        def add_check(row, text, attr, default=True):
            var = ctk.BooleanVar(value=default)
            ctk.CTkCheckBox(self.adv_frame, text=text, variable=var).grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(2, 6))
            setattr(self, attr, var)
            return var

        add_check(0, "到放号时刻再提交（否则立即提交）", "wait_var", True)

        ctk.CTkLabel(self.adv_frame, text="放号时刻", anchor="w").grid(
            row=1, column=0, sticky="w", pady=3)
        self.open_time_entry = ctk.CTkEntry(self.adv_frame)
        self.open_time_entry.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=3)
        self.open_time_entry.insert(0, DEFAULT_SETTINGS["open_time"])

        labels = [
            ("提交余量 (ms)", "margin_entry", DEFAULT_SETTINGS["margin_ms"]),
            ("请求间隔 (ms)", "interval_entry", DEFAULT_SETTINGS["interval_ms"]),
            ("最大尝试次数", "tries_entry", DEFAULT_SETTINGS["max_tries"]),
            ("放弃时间 (秒)", "giveup_entry", DEFAULT_SETTINGS["give_up_seconds"]),
        ]
        for i, (text, attr, default) in enumerate(labels, start=2):
            ctk.CTkLabel(self.adv_frame, text=text, anchor="w").grid(
                row=i, column=0, sticky="w", pady=3)
            entry = ctk.CTkEntry(self.adv_frame)
            entry.grid(row=i, column=1, sticky="ew", padx=(10, 0), pady=3)
            entry.insert(0, str(default))
            setattr(self, attr, entry)

        add_check(6, "无头模式（不显示 Chrome 窗口）", "headless_var", False)

        ctk.CTkLabel(self.adv_frame,
                     text="提示：默认放号 20:00，图书馆通常在此时放出后天的座位。\n"
                          "间隔 3300ms 是实测安全值，改小可能触发限流。",
                     anchor="w", justify="left", text_color="gray50",
                     font=ctk.CTkFont(size=11)).grid(row=7, column=0, columnspan=2,
                                                     sticky="w", pady=(6, 2))

    # ---------- 交互 ----------
    def _toggle_seat_mode(self):
        custom = self.mode_seg.get() == "自定义座位"
        # 楼层始终可见：座位号只有结合楼层才能换算成接口内部的座位 ID
        self.room_label.grid()
        self.room_menu.grid()
        for i, (label, entry) in enumerate(self.tier_rows, start=2):
            if custom:
                label.grid(row=i, column=0, sticky="w", pady=4)
                entry.grid(row=i, column=1, sticky="ew", padx=(10, 0), pady=4)
            else:
                label.grid_remove()
                entry.grid_remove()
        if custom:
            self.custom_hint.grid(row=5, column=0, columnspan=2, sticky="w")
        else:
            self.custom_hint.grid_remove()
        self._refresh_seat_hint()

    def _refresh_seat_hint(self):
        """把提示更新为「该楼层可选 001–220」。"""
        room = self.rooms.get(self.room_menu.get())
        if not room:
            self.custom_hint.configure(text="按顺序尝试，支持逗号分隔与 a-b 区间")
        else:
            total = len(expand_ranges(room))
            self.custom_hint.configure(text="按顺序尝试，支持逗号分隔与 a-b 区间；"
                                            "该楼层可选 001-{:03d}".format(total))

    def _schedule_slot(self, day):
        """取某一天的时段（整数小时对）。"""
        row = self.day_rows[day]
        return (as_int(row["start"].get().split(":")[0], DEFAULT_SETTINGS["start_hour"]),
                as_int(row["end"].get().split(":")[0], DEFAULT_SETTINGS["end_hour"]))

    def _refresh_schedule_preview(self):
        days = [i for i, row in enumerate(self.day_rows) if row["var"].get()]
        if not self.sched_var.get():
            self.sched_hint.configure(text="未启用：点击「开始预约」后立即执行")
            return
        if not days:
            self.sched_hint.configure(text="请至少勾选一个星期")
            return
        target = next_schedule_time(days, self.sched_time.get().strip())
        if target is None:
            self.sched_hint.configure(text="时刻格式应为 HH:MM:SS，例如 19:55:00")
            return
        start, end = self._schedule_slot(target.weekday())
        if end <= start:
            self.sched_hint.configure(text="{} 的结束时间必须晚于开始时间".format(
                WEEKDAYS[target.weekday()]))
            return
        self.sched_hint.configure(text="下次自动抢座：{} {} {}，该天时段 {:02d}:00–{:02d}:00".format(
            target.strftime("%Y-%m-%d"), WEEKDAYS[target.weekday()],
            target.strftime("%H:%M:%S"), start, end))

    def _toggle_advanced(self):
        self.advanced_open = not self.advanced_open
        if self.advanced_open:
            self.adv_frame.pack(fill="x", padx=16, pady=(0, 14))
            self.adv_toggle.configure(text="高级设置  ▾")
        else:
            self.adv_frame.pack_forget()
            self.adv_toggle.configure(text="高级设置  ▸")

    def _refresh_preview(self):
        days = self._days_ahead()
        date = target_date(days)
        start = as_int(self.start_hour.get().split(":")[0], 10)
        end = as_int(self.end_hour.get().split(":")[0], 22)
        hours = end - start
        text = "{} {}  {:02d}:00–{:02d}:00".format(date, weekday_name(days), start, end)
        text += "（{} 小时）".format(hours) if hours > 0 else "（时段无效）"
        self.preview_label.configure(text=text)

    def _days_ahead(self):
        return 1 if self.date_seg.get() == "明天" else 2

    def _apply_saved(self):
        st = self.saved
        self.id_entry.insert(0, str(st.get("student_id") or ""))
        if st.get("remember_password") and st.get("password"):
            self.pwd_entry.insert(0, str(st.get("password")))
            self.remember_var.set(True)
        self.date_seg.set("后天" if int(st.get("days_ahead") or 2) == 2 else "明天")
        self.start_hour.set("{:02d}:00".format(int(st.get("start_hour") or 10)))
        self.end_hour.set("{:02d}:00".format(int(st.get("end_hour") or 22)))
        if st.get("seat_mode") == "custom":
            self.mode_seg.set("自定义座位")
        if st.get("room") in self.rooms:
            self.room_menu.set(st["room"])
        tiers = st.get("custom_tiers") or []
        if not any(tiers):
            legacy = st.get("custom_seats") or []
            tiers = [legacy] if legacy else []
        for i, (_label, entry) in enumerate(self.tier_rows):
            seats = tiers[i] if i < len(tiers) else []
            if seats:
                entry.insert(0, ",".join("{:03d}".format(int(s)) for s in seats))
        self._toggle_seat_mode()
        self.sched_var.set(bool(st.get("schedule_enabled", False)))
        saved_days = {int(d) for d in (st.get("schedule_days") or [])
                      if str(d).lstrip("-").isdigit()}
        saved_hours = st.get("schedule_hours") or {}
        fallback = (int(st.get("start_hour") or DEFAULT_SETTINGS["start_hour"]),
                    int(st.get("end_hour") or DEFAULT_SETTINGS["end_hour"]))
        for i, row in enumerate(self.day_rows):
            row["var"].set(i in saved_days)
            pair = None
            if isinstance(saved_hours, dict):
                pair = saved_hours.get(str(i), saved_hours.get(i))
            elif isinstance(saved_hours, (list, tuple)) and i < len(saved_hours):
                pair = saved_hours[i]
            try:
                start, end = int(pair[0]), int(pair[1])
            except (TypeError, ValueError, IndexError, KeyError):
                start, end = fallback
            row["start"].set("{:02d}:00".format(start))
            row["end"].set("{:02d}:00".format(end))
        if st.get("schedule_time"):
            self.sched_time.delete(0, "end")
            self.sched_time.insert(0, str(st["schedule_time"]))
        self._refresh_schedule_preview()
        self.wait_var.set(bool(st.get("wait_until_open", True)))
        if st.get("open_time"):
            self.open_time_entry.delete(0, "end")
            self.open_time_entry.insert(0, str(st["open_time"]))
        for attr, key in (("margin_entry", "margin_ms"), ("interval_entry", "interval_ms"),
                          ("tries_entry", "max_tries"), ("giveup_entry", "give_up_seconds")):
            if st.get(key) is not None:
                getattr(self, attr).delete(0, "end")
                getattr(self, attr).insert(0, str(st[key]))
        self.extend_var.set(bool(st.get("extend_range", True)))
        self.headless_var.set(bool(st.get("headless", False)))

    # ---------- 启动 / 停止 ----------
    def _collect(self):
        mode = "custom" if self.mode_seg.get() == "自定义座位" else "room"
        st = {
            "student_id": self.id_entry.get().strip(),
            "password": self.pwd_entry.get(),
            "remember_password": bool(self.remember_var.get()),
            "days_ahead": self._days_ahead(),
            "start_hour": as_int(self.start_hour.get().split(":")[0], 10),
            "end_hour": as_int(self.end_hour.get().split(":")[0], 22),
            "seat_mode": mode,
            "room": self.room_menu.get(),
            "custom_tiers": ([parse_seat_text(entry.get()) for _label, entry in self.tier_rows]
                             if mode == "custom" else []),
            "custom_seats": [],
            "schedule_enabled": bool(self.sched_var.get()),
            "schedule_days": [i for i, row in enumerate(self.day_rows) if row["var"].get()],
            "schedule_hours": {str(i): list(self._schedule_slot(i))
                               for i, row in enumerate(self.day_rows) if row["var"].get()},
            "schedule_time": self.sched_time.get().strip() or DEFAULT_SETTINGS["schedule_time"],
            "wait_until_open": bool(self.wait_var.get()),
            "open_time": self.open_time_entry.get().strip(),
            "margin_ms": as_int(self.margin_entry.get(), DEFAULT_SETTINGS["margin_ms"]),
            "interval_ms": as_int(self.interval_entry.get(), DEFAULT_SETTINGS["interval_ms"]),
            "max_tries": as_int(self.tries_entry.get(), DEFAULT_SETTINGS["max_tries"]),
            "give_up_seconds": as_int(self.giveup_entry.get(), DEFAULT_SETTINGS["give_up_seconds"]),
            "extend_range": bool(self.extend_var.get()),
            "headless": bool(self.headless_var.get()),
        }
        return st

    def _validate(self, st):
        if not st["student_id"]:
            return "请填写学号"
        if not st["password"]:
            return "请填写密码"
        if st["end_hour"] <= st["start_hour"]:
            return "结束时间必须晚于开始时间"
        room = self.rooms.get(st["room"])
        if not room:
            return "请选择楼层"
        if st["seat_mode"] == "custom":
            numbers = [n for tier in st["custom_tiers"] for n in tier]
            if not numbers:
                return "请至少填写一个自定义座位号（首选）"
            total = len(expand_ranges(room))
            if any(n > 999 for n in numbers):
                return ("座位号请填座位图上的 3 位号码（001-{:03d}），"
                        "不要填系统内部的 5 位 ID".format(total))
            for name, tier in zip(("首选", "次选", "备用"), st["custom_tiers"]):
                bad = [n for n in tier if not 1 <= n <= total]
                if bad:
                    return "{}座位号 {} 超出「{}」范围，应为 001-{:03d}".format(
                        name, "、".join(str(n) for n in bad[:5]), room.get("title"), total)
        if st["schedule_enabled"]:
            if not st["schedule_days"]:
                return "已启用定时，请至少勾选一个星期"
            for day, pair in st["schedule_hours"].items():
                if pair[1] <= pair[0]:
                    return "{} 的结束时间必须晚于开始时间".format(WEEKDAYS[int(day)])
            if next_schedule_time(st["schedule_days"], st["schedule_time"]) is None:
                return "抢座时刻格式不对，应为 HH:MM:SS，例如 19:55:00"
        return None

    def _start(self):
        if self.worker and self.worker.is_alive():
            return
        st = self._collect()
        error = self._validate(st)
        if error:
            messagebox.showwarning("请检查设置", error, parent=self)
            return

        self._save_settings(st)
        self._clear_log()
        self._set_running(True)
        self._append("===== 开始预约 =====")

        self.stop_event = threading.Event()
        self.booker = SeatBooker(st, log=self._append_threadsafe, stop_event=self.stop_event)
        self.worker = threading.Thread(target=self._run_worker, daemon=True)
        self.worker.start()

    def _run_worker(self):
        results = []
        try:
            results = self.booker.run()
        except Exception as e:
            self._append_threadsafe("运行异常：{}".format(e))
        finally:
            try:
                self.booker.close()
            except Exception:
                pass
        try:
            self.after(0, self._finish, results)
        except Exception:
            pass

    @staticmethod
    def _succeeded(result):
        result = result or {}
        return "成功" in str(result.get("message") or "") \
            or str(result.get("code")).lower() == "ok"

    def _finish(self, results):
        self._set_running(False)
        results = results or []
        success = next((r for r in reversed(results) if self._succeeded(r)), None)
        result = success or (results[-1] if results else None)
        if result:
            seat = result.get("seat")
            if success:
                label = ""
                if seat:
                    room = room_of_seat(self.rooms, seat)
                    number = seat_number(room, seat) if room else None
                    label = "（座位 {:03d}）".format(number) if number else "（座位 {}）".format(seat)
                self._set_status("● 预约成功" + label, "#2f9e44")
            else:
                self._set_status("● 未成功，请查看日志", "#c05621")
        else:
            self._set_status("● 已结束，请查看日志", "gray50")
        self._append("===== 运行结束 =====")

    def _stop(self):
        if self.stop_event is None:
            return
        self.stop_event.set()
        self._set_status("● 正在停止…", "#c05621")
        self._append("收到停止指令，正在结束当前任务…")

    def _set_running(self, running):
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        if running:
            self._set_status("● 运行中", "#2f9e44")

    def _set_status(self, text, color="gray50"):
        self.status_label.configure(text=text, text_color=color)

    # ---------- 日志 ----------
    def _append_threadsafe(self, msg):
        try:
            self.after(0, self._append, msg)
        except Exception:
            pass

    def _append(self, msg):
        if not self.winfo_exists():
            return
        line = "[{}] {}\n".format(datetime.now().strftime("%H:%M:%S"), msg)
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    # ---------- 在线更新 ----------
    def _check_update(self, silent=False):
        if self._checking_update or self._downloading:
            return
        self._checking_update = True
        self.update_btn.configure(state="disabled", text="检查中…")
        threading.Thread(target=self._check_update_worker, args=(silent,),
                         daemon=True).start()

    def _check_update_worker(self, silent):
        info = updater.check_latest(VERSION)
        try:
            self.after(0, self._on_update_checked, info, silent)
        except Exception:
            pass

    def _on_update_checked(self, info, silent):
        self._checking_update = False
        if not self.winfo_exists():
            return
        self.update_btn.configure(state="normal", text="检查更新")
        if not info:
            self._append("已是最新版本 v{}".format(VERSION))
            if not silent:
                messagebox.showinfo("检查更新",
                                    "当前已是最新版本 v{}。".format(VERSION), parent=self)
            return

        self._append("发现新版本 v{}（当前 v{}）".format(info["version"], VERSION))
        notes = (info.get("notes") or "").strip()
        if len(notes) > 600:
            notes = notes[:600] + "…"
        msg = "发现新版本 v{}（当前 v{}）。\n\n{}\n\n是否立即下载并更新？".format(
            info["version"], VERSION, notes or "（该版本没有更新说明）")
        if not messagebox.askyesno("发现新版本", msg, parent=self):
            return

        if not getattr(sys, "frozen", False):
            messagebox.showinfo("提示", "当前为源码运行模式，无法自动替换文件。\n"
                                        "将打开下载页面，请手动下载最新 exe。", parent=self)
            self._open_release_page(info)
            return
        if not info.get("has_asset"):
            messagebox.showinfo("提示", "该版本未附带 exe 文件，请前往发布页手动下载。",
                                parent=self)
            self._open_release_page(info)
            return
        self._start_download(info)

    def _open_release_page(self, info):
        try:
            webbrowser.open(info.get("page") or updater.RELEASES_URL)
        except Exception:
            self._append((info.get("page") or updater.RELEASES_URL))

    def _start_download(self, info):
        if self._downloading:
            return
        self._downloading = True
        self.update_btn.configure(state="disabled", text="更新中…")
        self._append("开始下载 v{}（{:.1f} MB）…".format(
            info["version"], (info.get("size") or 0) / 1048576.0))
        threading.Thread(target=self._download_worker, args=(info,), daemon=True).start()

    def _download_worker(self, info):
        dest = os.path.join(tempfile.gettempdir(), "HDU_Lib_AutoBook_update.exe")
        state = {"pct": -1}

        def progress(done, total):
            if not total:
                return
            pct = int(done * 100 / total)
            if pct >= state["pct"] + 5 or pct == 100:
                state["pct"] = pct
                self._append_threadsafe("下载中 {}%".format(pct))

        try:
            updater.download(info["url"], dest, progress=progress)
        except Exception as e:
            try:
                self.after(0, self._on_download_failed, str(e))
            except Exception:
                pass
            return
        try:
            self.after(0, self._on_downloaded, info, dest)
        except Exception:
            pass

    def _on_download_failed(self, err):
        self._downloading = False
        if not self.winfo_exists():
            return
        self.update_btn.configure(state="normal", text="检查更新")
        self._append("下载失败：{}".format(err))
        messagebox.showerror("更新失败", "下载更新失败：\n{}".format(err), parent=self)

    def _on_downloaded(self, info, dest):
        self._downloading = False
        if not self.winfo_exists():
            return
        self.update_btn.configure(state="normal", text="检查更新")
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("请稍后", "预约任务正在运行，请等任务结束后再更新。", parent=self)
            return
        if not messagebox.askyesno(
                "更新就绪",
                "新版本 v{} 已下载完成。\n程序将退出，自动替换文件并重新启动，是否继续？".format(
                    info["version"]), parent=self):
            return
        try:
            updater.apply_update(dest)
        except Exception as e:
            messagebox.showerror("更新失败", "无法启动更新程序：\n{}".format(e), parent=self)
            return
        self._append("正在重启以完成更新…")
        self.destroy()

    # ---------- 其它 ----------
    def _open_data_dir(self):
        path = paths.data_dir()
        try:
            os.startfile(path)  # noqa: 仅 Windows
        except AttributeError:
            self._append(path)

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("确认退出", "预约任务正在运行，确定要退出吗？", parent=self):
                return
            self.stop_event.set()
        self.destroy()


def main():
    # 打包为无控制台程序时 stdout/stderr 为 None，部分库打印日志会报错
    if sys.stdout is None or sys.stderr is None:
        devnull = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = sys.stdout or devnull
        sys.stderr = sys.stderr or devnull
    AutoBookApp().mainloop()


if __name__ == "__main__":
    main()
