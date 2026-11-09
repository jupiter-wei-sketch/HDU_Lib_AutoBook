# -*- coding: utf-8 -*-
"""杭电图书馆座位预约核心逻辑（与界面完全解耦，可单独调用）。

设计要点：
1. 放号前提前登录并预热连接，到点只发一次纯 POST；
2. 用 HTTP Date 多次采样估算与本机的时钟偏移，取区间下界，宁可晚一点点也绝不提前；
3. 服务端对 bookSeats 有限流（实测约 3 秒放行一次），因此主动按固定节奏发送；
4. 首选座位被占后按距离扩展到同层其它座位，避免空转。
"""
import os
import socket
import threading
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests
import yaml
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
# 直接导入具体模块，避免 selenium 4.x 的懒加载在 PyInstaller 打包后找不到模块
from selenium.webdriver.chrome.webdriver import WebDriver as Chrome
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from .paths import resource_path

BASE = "https://hdu.huitu.zhishulib.com"
BOOK_URL = BASE + "/Seat/Index/bookSeats?LAB_JSON=1"
USER_INFO_URL = BASE + "/Seat/Index/searchSeats?LAB_JSON=1"

EPOCH_BASE = datetime(1970, 1, 1, 8, 0, 0)  # 接口的 beginTime 以此为零点
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
TIER_NAMES = ("首选", "次选", "备用")

HEADERS = {
    "Host": "hdu.huitu.zhishulib.com",
    "Accept": "application/json, text/plain, */*",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    "Origin": BASE,
    "Referer": BASE + "/",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

ALREADY_BOOKED_KEYS = ("已有预约", "重复预约", "请先取消", "当前已有", "只能预约一个", "已存在预约")
SEAT_TAKEN_KEYS = ("已被", "占用", "不可用", "不存在", "冲突", "无法预约")
NOT_OPEN_KEYS = ("未开放", "未到", "不在预约", "尚未开始", "未开始", "超出可预约")
RATE_LIMIT_KEYS = ("频繁", "人数过多")

DEFAULT_SETTINGS = {
    "student_id": "",
    "password": "",
    "remember_password": False,
    "days_ahead": 2,          # 1=明天，2=后天
    "start_hour": 10,         # 时段开始（24 小时制）
    "end_hour": 22,           # 时段结束（不含）
    "seat_mode": "room",      # room=整层；custom=自定义座位号
    "room": "",               # 房间全称，如「宋韵云图（四楼）」
    "custom_tiers": [],       # 自定义座位号分档：[[首选...], [次选...], [备用...]]
    "custom_seats": [],       # 旧版单档自定义座位号（仍兼容读取）
    "schedule_enabled": False,    # 定时抢座（闹钟式）
    "schedule_days": [],          # 0=周一 … 6=周日
    "schedule_time": "19:55:00",  # 每周这些天的这个时刻自动开始
    "schedule_hours": {},         # 每个星期各自的使用时段：{"0": [10, 22], ...}
    "wait_until_open": True,  # 是否等到放号时刻再提交
    "open_time": "20:00:00",
    "margin_ms": 120,
    "interval_ms": 3300,
    "max_tries": 180,
    "give_up_seconds": 600,
    "extend_range": True,
    "headless": False,
}


def load_rooms():
    """读取座位表，返回 {房间全称: 配置}；短名与全称指向同一份配置，这里按标题去重。"""
    path = resource_path(os.path.join("config", "seat_config.yml"))
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    rooms, seen = {}, set()
    for name, cfg in raw.items():
        if not isinstance(cfg, dict) or "ranges" not in cfg:
            continue
        title = cfg.get("title") or name
        if title in seen:
            continue
        seen.add(title)
        rooms[title] = cfg
    return rooms


def expand_ranges(cfg):
    """把 ranges 展开成内部座位 ID 列表（升序），并剔除 exclude 里不存在的 ID。"""
    excluded = {int(x) for x in (cfg.get("exclude") or [])}
    seats = []
    for begin, end in cfg["ranges"]:
        seats.extend(i for i in range(int(begin), int(end) + 1) if i not in excluded)
    return seats


def seat_number(cfg, seat_id):
    """内部 ID → 真实的 3 位座位号（房间内按 ID 升序从 001 编号）。找不到返回 None。"""
    try:
        return expand_ranges(cfg).index(int(seat_id)) + 1
    except (ValueError, TypeError):
        return None


def seat_id_of_number(cfg, number):
    """真实座位号 → 内部 ID。超出范围返回 None。"""
    try:
        number = int(number)
    except (TypeError, ValueError):
        return None
    seats = expand_ranges(cfg)
    if 1 <= number <= len(seats):
        return seats[number - 1]
    return None


def room_of_seat(rooms, seat):
    seat = int(seat)
    for cfg in rooms.values():
        if seat in {int(x) for x in (cfg.get("exclude") or [])}:
            continue
        for begin, end in cfg["ranges"]:
            if int(begin) <= seat <= int(end):
                return cfg
    return None


def target_date(days_ahead):
    return (datetime.now() + timedelta(days=int(days_ahead))).date()


def weekday_name(days_ahead):
    return WEEKDAYS[(datetime.now().weekday() + int(days_ahead)) % 7]


def next_schedule_time(days, time_text, now=None):
    """下次「每周 days 的 time_text」时刻；days 为 0(周一)~6(周日)。

    days 为空、时刻格式错误或星期越界时返回 None。
    """
    try:
        days = {int(d) for d in days if 0 <= int(d) <= 6}
    except (TypeError, ValueError):
        return None
    if not days:
        return None
    now = now or datetime.now()
    try:
        hh, mm, ss = [int(x) for x in str(time_text).split(":")]
        base = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    except ValueError:
        return None
    for ahead in range(0, 8):
        cand = base + timedelta(days=ahead)
        if cand.weekday() in days and cand > now:
            return cand
    return None


def normalize_slot(pair):
    """把 [开始, 结束] 归一到两个整数小时；格式或范围不对返回 None。"""
    try:
        start, end = int(pair[0]), int(pair[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if not (0 <= start < 24) or not (0 <= end <= 24) or end <= start:
        return None
    return [start, end]


class SeatBooker:
    """一次预约任务的执行器：登录 → 等放号 → 逐时段提交。"""

    def __init__(self, settings, log=None, stop_event=None):
        self.st = dict(DEFAULT_SETTINGS)
        self.st.update(settings or {})
        self._log = log or (lambda msg: print(msg))
        self.stop = stop_event or threading.Event()
        self.session = requests.Session()
        self.session.trust_env = False  # 忽略系统代理，统一直连
        self.driver = None
        self.cookie = None
        self.user_data = None
        self.rooms = load_rooms()
        self.results = []
        self.current_day = None  # 定时模式下的执行日（0=周一），用于取该天的时段

    # ---------- 基础工具 ----------
    def log(self, msg):
        self._log(msg)

    @staticmethod
    def _hms(ts=None, with_ms=False):
        dt = datetime.fromtimestamp(ts) if ts else datetime.now()
        return dt.strftime("%H:%M:%S.%f")[:-3] if with_ms else dt.strftime("%H:%M:%S")

    def _sleep_until(self, ts):
        """睡到指定时间戳；每 0.2 秒检查一次是否被手动停止。返回 False 表示已停止。"""
        while True:
            if self.stop.is_set():
                return False
            remain = ts - time.time()
            if remain <= 0:
                return True
            if remain > 0.05:
                time.sleep(min(remain - 0.05, 0.2))
            else:
                while time.time() < ts:
                    if self.stop.is_set():
                        return False

    def _open_timestamp(self):
        text = str(self.st.get("open_time") or "").strip()
        if not self.st.get("wait_until_open", True) or not text:
            return None
        try:
            hh, mm, ss = [int(x) for x in text.split(":")]
        except ValueError:
            self.log("放号时刻「{}」格式不对，应为 HH:MM:SS，已改为立即提交".format(text))
            return None
        target = datetime.now().replace(hour=hh, minute=mm, second=ss, microsecond=0)
        return target.timestamp()

    # ---------- 登录 ----------
    def describe_network(self):
        local_ip = "未知"
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("223.5.5.5", 80))
            local_ip = sock.getsockname()[0]
            sock.close()
        except OSError:
            pass
        try:
            site_ip = socket.gethostbyname("hdu.huitu.zhishulib.com")
        except OSError:
            site_ip = "解析失败"
        campus = site_ip.startswith(("10.", "172.", "192.168."))
        self.log("网络环境：本机 {} → 站点 {} ｜ {}".format(
            local_ip, site_ip, "校园网内网直连" if campus else "公网直连"))

    def login(self):
        student_id = str(self.st.get("student_id") or "").strip()
        password = str(self.st.get("password") or "")
        if not student_id or not password:
            self.log("学号或密码为空")
            return False
        self.log("开始登录（{}）...".format(student_id))
        self.describe_network()

        try:
            opts = Options()
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--window-size=1280,900")
            if self.st.get("headless"):
                opts.add_argument("--headless=new")
            self.driver = Chrome(service=Service(), options=opts)
            wait = WebDriverWait(self.driver, 20, 0.5)

            self.driver.get(BASE + "/")
            wait.until(lambda d: "sso.hdu.edu.cn" in d.current_url)

            form_wait = WebDriverWait(self.driver, 30, 0.5)
            user_el = form_wait.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "input[name='username']:not([type='hidden'])")))

            # 关掉公告弹窗（存在才关）
            for btn in self.driver.find_elements(By.CSS_SELECTOR, "img.icon-close, .ant-modal-close"):
                try:
                    if btn.is_displayed() and btn.is_enabled():
                        btn.click()
                        WebDriverWait(self.driver, 3, 0.3).until(EC.invisibility_of_element(btn))
                except Exception:
                    pass

            user_el.clear()
            user_el.send_keys(student_id)
            pwd_el = form_wait.until(EC.visibility_of_element_located(
                (By.CSS_SELECTOR, "input[type='password']")))
            pwd_el.clear()
            pwd_el.send_keys(password)
            pwd_el.send_keys(Keys.TAB)  # 失焦后再点登录，避免按钮仍是 disabled
            form_wait.until(lambda d: "disabled" not in d.find_element(
                By.CSS_SELECTOR, "button.login-button").get_attribute("class"))
            self.driver.find_element(By.CSS_SELECTOR, "button.login-button").click()

            WebDriverWait(self.driver, 30, 0.5).until(
                lambda d: urlsplit(d.current_url).hostname == "hdu.huitu.zhishulib.com")
            time.sleep(3)
            cookies = self.driver.get_cookies()
            self.cookie = ";".join("{}={}".format(c["name"], c["value"]) for c in cookies)
            self.log("登录成功")
            return True
        except Exception as e:
            self.log("登录失败：{}".format(e))
            return False

    def get_user_info(self):
        try:
            headers = dict(HEADERS, Cookie=self.cookie)
            resp = self.session.get(USER_INFO_URL, headers=headers, timeout=10)
            self.user_data = resp.json()["DATA"]
            _ = self.user_data["uid"]
            self.log("已获取账号信息（uid {}）".format(self.user_data["uid"]))
            return True
        except Exception as e:
            self.log("获取账号信息失败：{}".format(e))
            return False

    # ---------- 校时与预热 ----------
    def _server_offset(self):
        """返回 服务器时间 - 本机时间（秒）。多次采样求可信区间交集并取下界，只可能偏小。"""
        lo, hi = -float("inf"), float("inf")
        hits = 0
        for i in range(8):
            if i:
                time.sleep(0.35)
            t0 = time.time()
            try:
                resp = self.session.head(BASE + "/", headers=dict(HEADERS, Cookie=self.cookie),
                                         timeout=5)
            except Exception:
                continue
            t1 = time.time()
            date_str = resp.headers.get("Date")
            if not date_str:
                continue
            try:
                srv = parsedate_to_datetime(date_str).timestamp()
            except Exception:
                continue
            lo = max(lo, srv - t1)
            hi = min(hi, srv + 1 - t0)
            hits += 1
        if not hits or hi <= lo:
            self.log("服务器校时失败，改按本机时间执行")
            return 0.0
        self.log("服务器校时：样本 {} 个，偏移区间 [{:+.3f}, {:+.3f}] 秒".format(hits, lo, hi))
        return lo

    def _prewarm(self):
        try:
            t0 = time.time()
            self.session.get(BASE + "/", headers=dict(HEADERS, Cookie=self.cookie), timeout=5)
            self.log("连接预热完成（{:.0f}ms）".format((time.time() - t0) * 1000))
        except Exception as e:
            self.log("连接预热失败（不影响预约）：{}".format(e))

    def wait_for_open_time(self):
        open_ts = self._open_timestamp()
        if open_ts is None:
            self._prewarm()
            return True
        remain = open_ts - time.time()
        if remain <= 0:
            self.log("已过放号时刻，立即预热并开抢")
            self._prewarm()
            return True

        self._prewarm()
        offset = self._server_offset()
        if not -30 <= offset <= 30:
            self.log("服务器时钟偏差异常（{:+.1f}s），改按本机时间".format(offset))
            offset = 0.0
        margin = float(self.st.get("margin_ms") or 120) / 1000
        target_ts = open_ts - offset + margin
        self.log("本机{}服务器 {:.2f}s，将在本机 {} 提交（余量 {:.0f}ms）".format(
            "快于" if offset < 0 else "慢于", abs(offset), self._hms(target_ts, True), margin * 1000))

        if not self._sleep_until(target_ts - 5):
            return False
        self._prewarm()
        if not self._sleep_until(target_ts):
            return False
        self.log("到达放号时刻 {}，立即提交预约".format(self._hms(with_ms=True)))
        return True

    # ---------- 预约 ----------
    def _custom_tiers(self):
        """把自定义座位号归一为 [[首选...], [次选...], [备用...]]，兼容旧版单档。"""
        tiers = self.st.get("custom_tiers") or []
        normalized = []
        for tier in tiers:
            numbers = []
            for n in (tier if isinstance(tier, (list, tuple, set)) else [tier]):
                try:
                    numbers.append(int(n))
                except (TypeError, ValueError):
                    continue
            normalized.append(numbers)
        if any(normalized):
            return normalized
        legacy = []
        for n in self.st.get("custom_seats") or []:
            try:
                legacy.append(int(n))
            except (TypeError, ValueError):
                continue
        return [legacy] if legacy else []

    def _candidate_seats(self):
        room = self.rooms.get(self.st.get("room"))
        if not room:
            return []
        if self.st.get("seat_mode") == "custom":
            tiers = self._custom_tiers()
            if not any(tiers):
                return []
            total = len(expand_ranges(room))
            ids, seen = [], set()
            for index, numbers in enumerate(tiers):
                name = TIER_NAMES[index] if index < len(TIER_NAMES) else "备选{}".format(index + 1)
                resolved = []
                for number in numbers:
                    seat = seat_id_of_number(room, number)
                    if seat is None:
                        self.log("{}座位号 {} 超出「{}」范围（001-{:03d}），已跳过".format(
                            name, number, room.get("title"), total))
                        continue
                    if seat in seen:
                        self.log("{}座位号 {:03d} 与前面重复，已忽略".format(name, number))
                        continue
                    seen.add(seat)
                    resolved.append(seat)
                if resolved:
                    self.log("{}：{}".format(name, self._seat_text(resolved)))
                ids.extend(resolved)
            return ids
        return expand_ranges(room)

    def _seat_label(self, seat):
        """内部 ID 转成给人看的座位号，例如「001（ID 62565）」。"""
        room = room_of_seat(self.rooms, seat)
        number = seat_number(room, seat) if room else None
        return "{:03d}（ID {}）".format(number, seat) if number else str(seat)

    def _seat_text(self, seats, limit=12):
        text = "、".join(self._seat_label(s) for s in seats[:limit])
        if len(seats) > limit:
            text += " …共 {} 个".format(len(seats))
        return text

    def _build_candidates(self, seats):
        ordered = list(seats)
        if not ordered or not self.st.get("extend_range", True):
            return ordered
        anchor = ordered[-1]
        room = room_of_seat(self.rooms, anchor)
        if not room:
            return ordered
        taken = set(ordered)
        extra = sorted((s for s in expand_ranges(room) if s not in taken),
                       key=lambda s: (abs(s - anchor), s))
        if extra:
            self.log("候选 {} 个，用尽后按距离扩展同层 {} 个".format(len(ordered), len(extra)))
        return ordered + extra

    def _book_seat(self, start, hours, seat):
        book_time = (datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                     + timedelta(days=int(self.st.get("days_ahead") or 2))
                     + timedelta(hours=int(start)))
        total_seconds = int((book_time - EPOCH_BASE).total_seconds())
        data = "beginTime={}&duration={}&&seats[0]={}&seatBookers[0]={}".format(
            total_seconds, 3600 * int(hours), seat, self.user_data["uid"])
        headers = dict(HEADERS, Cookie=self.cookie)
        self.log("{} POST 座位 {}（{}）".format(
            self._hms(with_ms=True), self._seat_label(seat), data))
        t0 = time.time()
        resp = self.session.post(BOOK_URL, data=data, headers=headers, timeout=8)
        payload = resp.json()
        self.log("{} ← {:.0f}ms {} {}".format(
            self._hms(with_ms=True), (time.time() - t0) * 1000,
            payload.get("CODE"), payload.get("MESSAGE")))
        return {"code": payload.get("CODE"), "message": payload.get("MESSAGE"), "seat": seat}

    def _book_slot(self, start, hours):
        candidates = self._build_candidates(self._candidate_seats())
        if not candidates:
            self.log("没有可用座位，请检查楼层/自定义座位设置")
            return None
        interval = float(self.st.get("interval_ms") or 3300) / 1000
        max_tries = int(self.st.get("max_tries") or 180)
        deadline = time.time() + float(self.st.get("give_up_seconds") or 600)
        next_allowed = 0.0
        seat_index = 0
        tries = 0
        result = None
        while tries < max_tries and seat_index < len(candidates):
            if self.stop.is_set():
                self.log("已手动停止")
                break
            if time.time() > deadline:
                self.log("已到放弃时间，本时段未抢到")
                break
            if not self._sleep_until(next_allowed):
                break
            seat = candidates[seat_index]
            tries += 1
            try:
                result = self._book_seat(start, hours, seat)
            except Exception as e:
                self.log("请求异常：{}（第 {} 次）".format(e, tries))
                next_allowed = time.time() + interval
                continue
            msg = str(result.get("message") or "")
            if any(k in msg for k in ALREADY_BOOKED_KEYS):
                self.log("该时段已有预约，跳过")
                return result
            next_allowed = time.time() + interval
            if any(k in msg for k in NOT_OPEN_KEYS):
                self.log("尚未开放，{:.1f} 秒后同一座位重试".format(interval))
                continue
            if any(k in msg for k in RATE_LIMIT_KEYS):
                self.log("触发限流，已按 {:.1f} 秒节流".format(interval))
                continue
            if any(k in msg for k in SEAT_TAKEN_KEYS):
                self.log("座位 {} 不可约，换下一个（{}/{}）".format(
                    self._seat_label(seat), seat_index + 2, len(candidates)))
                seat_index += 1
                continue
            return result
        return result

    def book_all(self):
        start, end = self._slot_hours()
        hours = max(1, end - start)
        days_ahead = self.st.get("days_ahead") or 2
        date = target_date(days_ahead)
        note = ""
        if self.current_day is not None:
            note = "（执行日 {} 的时段）".format(WEEKDAYS[self.current_day])
        self.log("目标：{} {} {:02d}:00-{:02d}:00（{} 小时）{}".format(
            date, weekday_name(days_ahead), start, end, hours, note))
        room = self.rooms.get(self.st.get("room"))
        if room:
            total = len(expand_ranges(room))
            if self.st.get("seat_mode") == "custom":
                parts = []
                for index, numbers in enumerate(self._custom_tiers()):
                    if not numbers:
                        continue
                    name = TIER_NAMES[index] if index < len(TIER_NAMES) else "备选{}".format(index + 1)
                    parts.append("{} {}".format(
                        name, ", ".join("{:03d}".format(n) for n in numbers)))
                wanted = " ｜ ".join(parts) or "（无有效座位号）"
                self.log("座位：{} 自定义 {}（可选 001-{:03d}）".format(
                    room.get("title"), wanted, total))
            else:
                self.log("座位：{} 全层 001-{:03d}".format(room.get("title"), total))
        result = self._book_slot(start, hours)
        return [result] if result else []

    # ---------- 任务入口 ----------
    def _schedule_hours(self):
        """把 schedule_hours 归一成 {星期: [开始, 结束]}，兼容字典与列表两种写法。"""
        raw = self.st.get("schedule_hours") or {}
        items = raw.items() if isinstance(raw, dict) else enumerate(raw)
        out = {}
        for key, pair in items:
            try:
                day = int(key)
            except (TypeError, ValueError):
                continue
            slot = normalize_slot(pair)
            if 0 <= day <= 6 and slot:
                out[day] = slot
        return out

    def _slot_hours(self):
        """本次要预约的时段：定时模式下用执行日的个性化时段，否则用全局时段。"""
        if self.current_day is not None:
            pair = self._schedule_hours().get(self.current_day)
            if pair:
                return pair
        return [int(self.st.get("start_hour") or 10), int(self.st.get("end_hour") or 22)]

    def _next_schedule(self):
        if not self.st.get("schedule_enabled"):
            return None
        return next_schedule_time(self.st.get("schedule_days"), self.st.get("schedule_time"))

    def run(self):
        """执行入口。

        开启定时时先等到「每周 X 的 HH:MM:SS」再开始，抢完一轮后继续等下一个时点
        （像闹钟一样按周重复）；未开启定时则等待放号时刻后执行一次。
        """
        while True:
            target = self._next_schedule()
            if target is not None:
                self.current_day = target.weekday()
                start, end = self._slot_hours()
                remain = (target - datetime.now()).total_seconds()
                self.log("已启用定时：{} {} {} 自动开始抢座（约 {:.0f} 分钟后），"
                         "该天时段 {:02d}:00-{:02d}:00".format(
                             target.strftime("%Y-%m-%d"), WEEKDAYS[target.weekday()],
                             target.strftime("%H:%M:%S"), max(remain, 0) / 60, start, end))
                if not self._sleep_until(target.timestamp()):
                    return self.results
                self.log("定时到点 {}，开始执行".format(self._hms(with_ms=True)))
            else:
                self.current_day = None
                open_ts = self._open_timestamp()
                if open_ts is not None and open_ts - time.time() > 150:
                    wait_min = (open_ts - time.time()) / 60
                    self.log("距离放号还有 {:.0f} 分钟，程序将在放号前约 2 分半自动登录".format(wait_min))
                    if not self._sleep_until(open_ts - 150):
                        return self.results
            if self.stop.is_set():
                return self.results
            if not self.login():
                return self.results
            if not self.get_user_info():
                return self.results
            if not self.wait_for_open_time():
                return self.results
            self.results.extend(self.book_all())
            self.close()
            if target is None:
                return self.results

    def close(self):
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
