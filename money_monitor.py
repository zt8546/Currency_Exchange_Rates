"""多币种 -> CNY 实时汇率监控（中行牌价）—— Tkinter 桌面前端。"""
import bisect
import threading
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import messagebox, ttk

import matplotlib
matplotlib.use("TkAgg")
matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False
import matplotlib.dates as mdates
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from boc_core import (
    BY_CODE, CURRENCIES, backfill_history, fetch_rate, init_db,
    insert_snapshot, load_config, save_config,
)

POLL_SECONDS = 60


def combo_label(c):
    return f'{c["label"]} {c["code"]}'


def make_copyable(parent, textvariable=None, text="", font=None,
                  foreground="#000", width=None, anchor="w"):
    """像 Label，但内容可以选中、Ctrl+C 复制。底层是 readonly tk.Entry。"""
    bg = ttk.Style().lookup("TFrame", "background") or "SystemButtonFace"
    just = {"w": "left", "e": "right", "center": "center"}.get(anchor, "left")
    e = tk.Entry(
        parent, font=font, fg=foreground, readonlybackground=bg,
        relief="flat", borderwidth=0, highlightthickness=0,
        cursor="xterm", justify=just,
    )
    if textvariable is not None:
        e.config(textvariable=textvariable)
    elif text:
        e.insert(0, text)
    e.config(state="readonly")
    if width is not None:
        e.config(width=width)
    return e


class App:
    def __init__(self, root):
        self.root = root
        root.title("外币 -> CNY 汇率监控（中行牌价）")
        root.geometry("960x680")

        self.conn = init_db()
        self.cfg = load_config()
        self.current_currency = self.cfg.get("current_currency", "CAD")
        if self.current_currency not in BY_CODE:
            self.current_currency = "CAD"
        self.latest_rate = None
        self.prev_rate = None
        self.alerted_high = False
        self.alerted_low = False
        self.stop_event = threading.Event()

        # 图表交互状态
        self._chart_xs = []         # list[datetime]
        self._chart_xs_nums = []    # list[float] for bisect
        self._chart_mids = []
        self._chart_buy = []
        self._chart_sell = []
        self._cross_v = None
        self._cross_h = None
        self._cross_text = None
        self._chart_dot = None      # 红圆点：当前吸附的数据点
        self._click_marker = None
        self._click_text = None
        # 拖动平移状态
        self._pan_origin = None
        self._pan_moved = False

        self._build_ui()
        self._show_last_from_db()
        self._refresh_chart()

        self.canvas.mpl_connect("motion_notify_event", self._on_mouse_motion)
        self.canvas.mpl_connect("axes_leave_event", self._on_axes_leave)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("button_release_event", self._on_release)

        threading.Thread(target=lambda: self._backfill(self.current_currency),
                         daemon=True).start()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- UI ----------

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=12)
        top.pack(side=tk.TOP, fill=tk.X)

        # 币种下拉
        sel_frame = ttk.Frame(top)
        sel_frame.pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(sel_frame, text="币种", font=("Microsoft YaHei", 9),
                  foreground="#555").pack(anchor="w")
        self.currency_combo = ttk.Combobox(
            sel_frame,
            values=[combo_label(c) for c in CURRENCIES],
            state="readonly",
            width=14,
            font=("Microsoft YaHei", 12),
        )
        self.currency_combo.set(combo_label(BY_CODE[self.current_currency]))
        self.currency_combo.bind("<<ComboboxSelected>>", self._on_currency_changed)
        self.currency_combo.pack()

        info = BY_CODE[self.current_currency]
        self.unit_var = tk.StringVar(value=f"{info['unit']} {info['code']} =")
        make_copyable(top, textvariable=self.unit_var,
                      font=("Microsoft YaHei", 18), width=10).pack(side=tk.LEFT)

        self.rate_var = tk.StringVar(value="--")
        self.change_var = tk.StringVar(value="")
        self.updated_var = tk.StringVar(value="尚未更新")
        self.source_var = tk.StringVar(value="")

        self.rate_label = make_copyable(top, textvariable=self.rate_var,
                                        font=("Microsoft YaHei", 28, "bold"),
                                        foreground="#0a6", width=9)
        self.rate_label.pack(side=tk.LEFT, padx=8)
        make_copyable(top, text="CNY", font=("Microsoft YaHei", 14),
                      width=4).pack(side=tk.LEFT)
        make_copyable(top, text="（中行折算价）", font=("Microsoft YaHei", 10),
                      foreground="#888", width=10).pack(side=tk.LEFT, padx=4)
        self.change_label = make_copyable(top, textvariable=self.change_var,
                                          font=("Microsoft YaHei", 14), width=22)
        self.change_label.pack(side=tk.LEFT, padx=12)

        right = ttk.Frame(top)
        right.pack(side=tk.RIGHT)
        make_copyable(right, textvariable=self.updated_var,
                      font=("Microsoft YaHei", 9), width=56,
                      anchor="e").pack(anchor="e")
        make_copyable(right, textvariable=self.source_var,
                      font=("Microsoft YaHei", 9), foreground="#888",
                      width=30, anchor="e").pack(anchor="e")

        self.prices_frame = ttk.LabelFrame(
            self.root,
            text=self._prices_frame_title(),
            padding=8,
        )
        self.prices_frame.pack(side=tk.TOP, fill=tk.X, padx=12, pady=(0, 6))

        self.price_vars = {
            "buy_remit": tk.StringVar(value="--"),
            "sell_remit": tk.StringVar(value="--"),
            "buy_cash": tk.StringVar(value="--"),
            "sell_cash": tk.StringVar(value="--"),
        }
        labels = [
            ("现汇买入价", "buy_remit", "你持外币汇款 -> CNY"),
            ("现汇卖出价", "sell_remit", "你用 CNY 买外币汇出"),
            ("现钞买入价", "buy_cash", "你持外币现钞 -> CNY"),
            ("现钞卖出价", "sell_cash", "你用 CNY 取外币现钞"),
        ]
        for i, (name, key, hint) in enumerate(labels):
            cell = ttk.Frame(self.prices_frame)
            cell.grid(row=0, column=i, padx=12, sticky="w")
            make_copyable(cell, text=name, font=("Microsoft YaHei", 9),
                          foreground="#555", width=12).pack(anchor="w")
            make_copyable(cell, textvariable=self.price_vars[key],
                          font=("Microsoft YaHei", 14, "bold"), width=10).pack(anchor="w")
            make_copyable(cell, text=hint, font=("Microsoft YaHei", 8),
                          foreground="#888", width=20).pack(anchor="w")

        ctrl = ttk.LabelFrame(self.root,
                              text="到价提醒（按显示单位的中行折算价；留空则不提醒） / 时间范围",
                              padding=8)
        ctrl.pack(side=tk.TOP, fill=tk.X, padx=12, pady=(0, 8))

        ttk.Label(ctrl, text="高于:").grid(row=0, column=0, padx=4)
        self.high_entry = ttk.Entry(ctrl, width=9)
        self.high_entry.grid(row=0, column=1, padx=4)
        ttk.Label(ctrl, text="低于:").grid(row=0, column=2, padx=4)
        self.low_entry = ttk.Entry(ctrl, width=9)
        self.low_entry.grid(row=0, column=3, padx=4)
        self._load_alerts_to_entries(self.current_currency)

        ttk.Button(ctrl, text="保存提醒", command=self.save_alerts).grid(
            row=0, column=4, padx=6)
        ttk.Separator(ctrl, orient="vertical").grid(
            row=0, column=5, sticky="ns", padx=12)
        ttk.Label(ctrl, text="时间范围:").grid(row=0, column=6, padx=(0, 4))
        self.range_var = tk.StringVar(value="24h")
        for i, label in enumerate(["1h", "24h", "7d", "30d", "1y", "全部"]):
            ttk.Radiobutton(ctrl, text=label, value=label, variable=self.range_var,
                            command=self._refresh_chart).grid(row=0, column=7 + i, padx=2)
        ttk.Button(ctrl, text="立即刷新", command=self.force_refresh).grid(
            row=0, column=14, padx=(16, 0))
        ttk.Button(ctrl, text="重置视图", command=self._reset_view).grid(
            row=0, column=15, padx=(8, 0))

        self.fig = Figure(figsize=(8, 4), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.root)
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True,
                                         padx=12, pady=(0, 8))

        self.status_var = tk.StringVar(value="就绪")
        status_frame = tk.Frame(self.root, relief=tk.SUNKEN, borderwidth=1)
        status_frame.pack(side=tk.BOTTOM, fill=tk.X)
        make_copyable(status_frame, textvariable=self.status_var,
                      font=("Microsoft YaHei", 9)).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=4, pady=1)

    def _prices_frame_title(self):
        info = BY_CODE[self.current_currency]
        return f"中国银行牌价（单位：{info['unit']} {info['code']} 兑 CNY）"

    def _load_alerts_to_entries(self, code):
        alert = self.cfg["alerts"].get(code, {})
        self.high_entry.delete(0, tk.END)
        if alert.get("high") is not None:
            self.high_entry.insert(0, str(alert["high"]))
        self.low_entry.delete(0, tk.END)
        if alert.get("low") is not None:
            self.low_entry.insert(0, str(alert["low"]))

    # ---------- 显示工具 ----------

    def _scale(self, code, per1):
        if per1 is None:
            return None
        return per1 * BY_CODE[code]["unit"]

    def _show_last_from_db(self):
        """从 DB 取当前币种最近一条，立刻填充 UI（启动 / 切换币种时用）。"""
        code = self.current_currency
        row = self.conn.execute(
            "SELECT ts, rate, buy_remit, buy_cash, sell_remit, sell_cash, source "
            "FROM rates WHERE currency = ? ORDER BY ts DESC LIMIT 1",
            (code,),
        ).fetchone()
        self.latest_rate = None
        self.prev_rate = None
        if not row:
            self.rate_var.set("--")
            self.change_var.set("")
            for v in self.price_vars.values():
                v.set("--")
            self.updated_var.set("尚未更新")
            self.source_var.set("")
            return
        ts, rate, br, bc, sr, sc, src = row
        self.rate_var.set(f"{self._scale(code, rate):.4f}")
        self.change_var.set("")
        self.price_vars["buy_remit"].set(
            f"{self._scale(code, br):.4f}" if br is not None else "--")
        self.price_vars["buy_cash"].set(
            f"{self._scale(code, bc):.4f}" if bc is not None else "--")
        self.price_vars["sell_remit"].set(
            f"{self._scale(code, sr):.4f}" if sr is not None else "--")
        self.price_vars["sell_cash"].set(
            f"{self._scale(code, sc):.4f}" if sc is not None else "--")
        self.updated_var.set(f"本地 {ts.replace('T', ' ')}（DB 缓存）")
        self.source_var.set(f"数据源 {src}")
        self.latest_rate = rate

    # ---------- 事件 ----------

    def _on_currency_changed(self, _event):
        label = self.currency_combo.get()
        code = next((c["code"] for c in CURRENCIES if combo_label(c) == label),
                    self.current_currency)
        if code == self.current_currency:
            return
        self.current_currency = code
        self.cfg["current_currency"] = code
        save_config(self.cfg)

        info = BY_CODE[code]
        self.unit_var.set(f"{info['unit']} {info['code']} =")
        self.prices_frame.configure(text=self._prices_frame_title())
        self._load_alerts_to_entries(code)
        self.alerted_high = False
        self.alerted_low = False

        self._show_last_from_db()
        self._refresh_chart()

        threading.Thread(target=lambda: self._backfill(code), daemon=True).start()
        self.status_var.set(f"已切换到 {code}，正在抓取最新…")
        threading.Thread(target=self._fetch_once, daemon=True).start()

    def save_alerts(self):
        try:
            high = self.high_entry.get().strip()
            low = self.low_entry.get().strip()
            high_v = float(high) if high else None
            low_v = float(low) if low else None
        except ValueError:
            messagebox.showerror("无效输入", "请填写数字，例如 4.88")
            return
        self.cfg["alerts"][self.current_currency] = {"high": high_v, "low": low_v}
        save_config(self.cfg)
        self.alerted_high = False
        self.alerted_low = False
        self.status_var.set(
            f"已保存 {self.current_currency} 提醒：高于 {high_v}，低于 {low_v}"
        )
        self._refresh_chart()

    def force_refresh(self):
        self.status_var.set("正在抓取…")
        threading.Thread(target=self._fetch_once, daemon=True).start()

    # ---------- 后台任务 ----------

    def _backfill(self, code):
        def report(msg):
            self.root.after(0, lambda: self.status_var.set(msg))
        try:
            n = backfill_history(self.conn, code, days=365, progress_cb=report)
            if n > 0:
                report(f"{code} 回填 {n} 条历史数据（Frankfurter）")
                self.root.after(0, self._refresh_chart)
            elif BY_CODE[code]["fr"]:
                report(f"{code} 历史数据已是最新")
        except Exception as e:
            report(f"{code} 历史回填失败: {e}")

    def _poll_loop(self):
        while not self.stop_event.is_set():
            self._fetch_once()
            self.stop_event.wait(POLL_SECONDS)

    def _fetch_once(self):
        try:
            result = fetch_rate(self.current_currency)
        except Exception as e:
            msg = f"获取失败: {e}"
            self.root.after(0, lambda: self.status_var.set(msg))
            return
        ts = datetime.now().isoformat(timespec="seconds")
        insert_snapshot(self.conn, ts, result.get("source", "?"), result["currencies"])
        self.root.after(0, lambda: self._on_new_data(result, ts))

    def _on_new_data(self, result, ts):
        cur = self.current_currency
        data = result["currencies"].get(cur)
        source = result.get("source", "?")
        if data is None or data.get("rate") is None:
            self.status_var.set(f"本轮抓取未拿到 {cur}，保留上次显示")
            return

        rate = data["rate"]
        self.prev_rate = self.latest_rate
        self.latest_rate = rate

        disp_rate = self._scale(cur, rate)
        self.rate_var.set(f"{disp_rate:.4f}")

        if self.prev_rate is not None:
            diff = self._scale(cur, rate - self.prev_rate)
            pct = (rate - self.prev_rate) / self.prev_rate * 100
            arrow = "↑" if diff > 0 else ("↓" if diff < 0 else "·")
            color = "#c33" if diff > 0 else ("#3a7" if diff < 0 else "#888")
            self.change_var.set(f"{arrow} {diff:+.4f} ({pct:+.2f}%)")
            self.change_label.configure(foreground=color)
        else:
            self.change_var.set("")

        for key, var in self.price_vars.items():
            v = data.get(key)
            var.set(f"{self._scale(cur, v):.4f}" if v is not None else "--")

        boc_time = result.get("boc_time")
        if boc_time:
            self.updated_var.set(f"中行发布 {boc_time}   本地 {ts.replace('T', ' ')}")
        else:
            self.updated_var.set(f"更新时间 {ts.replace('T', ' ')}")
        self.source_var.set(f"数据源 {source}")

        if self.prev_rate is not None and rate == self.prev_rate:
            self.status_var.set(
                f"已抓取，与上次一致（{datetime.now().strftime('%H:%M:%S')}）")
        else:
            self.status_var.set(
                f"已更新（{datetime.now().strftime('%H:%M:%S')}）")

        self._refresh_chart()
        self._check_alerts(disp_rate)

    def _check_alerts(self, disp_rate):
        alert = self.cfg["alerts"].get(self.current_currency, {})
        high = alert.get("high")
        low = alert.get("low")
        if high is not None:
            if disp_rate >= high and not self.alerted_high:
                self.alerted_high = True
                self.root.bell()
                messagebox.showinfo(
                    "汇率到价",
                    f"{self.current_currency} 当前 {disp_rate:.4f} 已 ≥ 目标 {high}",
                )
            elif disp_rate < high:
                self.alerted_high = False
        if low is not None:
            if disp_rate <= low and not self.alerted_low:
                self.alerted_low = True
                self.root.bell()
                messagebox.showinfo(
                    "汇率到价",
                    f"{self.current_currency} 当前 {disp_rate:.4f} 已 ≤ 目标 {low}",
                )
            elif disp_rate > low:
                self.alerted_low = False

    # ---------- 图表 ----------

    def _range_cutoff(self):
        r = self.range_var.get()
        now = datetime.now()
        return {
            "1h": now - timedelta(hours=1),
            "24h": now - timedelta(hours=24),
            "7d": now - timedelta(days=7),
            "30d": now - timedelta(days=30),
            "1y": now - timedelta(days=365),
        }.get(r)

    def _refresh_chart(self):
        code = self.current_currency
        cutoff = self._range_cutoff()
        if cutoff is None:
            rows = self.conn.execute(
                "SELECT ts, rate, buy_remit, sell_remit FROM rates "
                "WHERE currency = ? ORDER BY ts",
                (code,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT ts, rate, buy_remit, sell_remit FROM rates "
                "WHERE currency = ? AND ts >= ? ORDER BY ts",
                (code, cutoff.isoformat(timespec="seconds")),
            ).fetchall()
        self.ax.clear()
        # 清空十字线/点击标记（ax.clear 已经删掉了 artist，置 None 让事件回调重建）
        self._cross_v = None
        self._cross_h = None
        self._cross_text = None
        self._chart_dot = None
        self._click_marker = None
        self._click_text = None
        unit = BY_CODE[code]["unit"]
        if rows:
            xs = [datetime.fromisoformat(r[0]) for r in rows]
            mids = [r[1] * unit for r in rows]
            buy = [(r[2] * unit) if r[2] is not None else None for r in rows]
            sell = [(r[3] * unit) if r[3] is not None else None for r in rows]
            self._chart_xs = xs
            self._chart_xs_nums = [mdates.date2num(x) for x in xs]
            self._chart_mids = mids
            self._chart_buy = buy
            self._chart_sell = sell

            self.ax.plot(xs, mids, "-", color="#0a6", linewidth=1.4,
                         label="中行折算价")
            self.ax.scatter([xs[-1]], [mids[-1]], color="#0a6", zorder=5)

            buy_pts = [(x, v) for x, v in zip(xs, buy) if v is not None]
            sell_pts = [(x, v) for x, v in zip(xs, sell) if v is not None]
            if buy_pts:
                bx, bv = zip(*buy_pts)
                self.ax.plot(bx, bv, "-", color="#36a", linewidth=0.9,
                             alpha=0.7, label="现汇买入价")
            if sell_pts:
                sx, sv = zip(*sell_pts)
                self.ax.plot(sx, sv, "-", color="#c63", linewidth=0.9,
                             alpha=0.7, label="现汇卖出价")

            self.ax.set_title(
                f"{code}/CNY 中行折算价（{unit} {code}）   最新 {mids[-1]:.4f}"
                f"   高 {max(mids):.4f}   低 {min(mids):.4f}   样本 {len(mids)}"
            )
            self.ax.grid(True, alpha=0.3)
            alert = self.cfg["alerts"].get(code, {})
            if alert.get("high") is not None:
                self.ax.axhline(alert["high"], color="#c33", linestyle="--",
                                linewidth=0.8, label=f"提醒高 {alert['high']}")
            if alert.get("low") is not None:
                self.ax.axhline(alert["low"], color="#3a7", linestyle="--",
                                linewidth=0.8, label=f"提醒低 {alert['low']}")
            self.ax.legend(loc="best", fontsize=8)
            self.fig.autofmt_xdate()
        else:
            self._chart_xs = []
            self._chart_xs_nums = []
            self._chart_mids = []
            self._chart_buy = []
            self._chart_sell = []
            self.ax.text(0.5, 0.5, f"{code} 暂无数据，等待第一次抓取…",
                         ha="center", va="center",
                         transform=self.ax.transAxes, color="#888")
        self.fig.tight_layout()
        self.canvas.draw_idle()

    def _reset_view(self):
        self._refresh_chart()
        self.status_var.set("已重置缩放与平移")

    # ---------- 图表鼠标交互 ----------

    def _nearest_index(self, x_num):
        if not self._chart_xs_nums:
            return None
        idx = bisect.bisect_left(self._chart_xs_nums, x_num)
        if idx >= len(self._chart_xs_nums):
            idx = len(self._chart_xs_nums) - 1
        if idx > 0 and abs(self._chart_xs_nums[idx - 1] - x_num) < abs(
                self._chart_xs_nums[idx] - x_num):
            idx -= 1
        return idx

    def _ensure_crosshair(self):
        if self._cross_v is None:
            # 关键：用当前坐标轴范围内的坐标初始化，并在创建后恢复 xlim/ylim。
            # 否则 axvline(0) / axhline(0) 会把 0 当数据点，把坐标轴拉爆到 1970 起步。
            xl = self.ax.get_xlim()
            yl = self.ax.get_ylim()
            x0, y0 = xl[0], yl[0]
            self._cross_v = self.ax.axvline(x0, color="#888", linewidth=0.7,
                                            alpha=0.7, linestyle=":")
            self._cross_h = self.ax.axhline(y0, color="#888", linewidth=0.7,
                                            alpha=0.7, linestyle=":")
            self._cross_text = self.ax.annotate(
                "", xy=(x0, y0), xytext=(12, 12), textcoords="offset points",
                bbox=dict(boxstyle="round,pad=0.4", fc="#fffdd0",
                          ec="#888", alpha=0.95),
                fontsize=9, zorder=20,
            )
            self.ax.set_xlim(xl)
            self.ax.set_ylim(yl)
            self._cross_v.set_visible(False)
            self._cross_h.set_visible(False)
            self._cross_text.set_visible(False)

    def _hide_crosshair(self):
        changed = False
        if self._cross_v is not None and self._cross_v.get_visible():
            self._cross_v.set_visible(False)
            self._cross_h.set_visible(False)
            self._cross_text.set_visible(False)
            changed = True
        if self._chart_dot is not None and self._chart_dot.get_visible():
            self._chart_dot.set_visible(False)
            changed = True
        if changed:
            self.canvas.draw_idle()

    def _on_mouse_motion(self, event):
        # —— 1. 拖动平移优先 ——
        if self._pan_origin is not None:
            ox = self._pan_origin
            dx_pix = (event.x or ox["x_pix"]) - ox["x_pix"]
            dy_pix = (event.y or ox["y_pix"]) - ox["y_pix"]
            if abs(dx_pix) + abs(dy_pix) > 3:
                self._pan_moved = True
            bbox = self.ax.bbox
            if bbox.width <= 0 or bbox.height <= 0:
                return
            x_per_pix = (ox["xlim"][1] - ox["xlim"][0]) / bbox.width
            y_per_pix = (ox["ylim"][1] - ox["ylim"][0]) / bbox.height
            self.ax.set_xlim(ox["xlim"][0] - dx_pix * x_per_pix,
                             ox["xlim"][1] - dx_pix * x_per_pix)
            self.ax.set_ylim(ox["ylim"][0] - dy_pix * y_per_pix,
                             ox["ylim"][1] - dy_pix * y_per_pix)
            self._hide_crosshair()
            self.canvas.draw_idle()
            return

        # —— 2. 正常十字光标 ——
        if event.inaxes != self.ax or event.xdata is None or event.ydata is None:
            self._hide_crosshair()
            return
        self._ensure_crosshair()
        cx = event.xdata
        cy = event.ydata
        # V/H 线自由跟随光标 —— 不再吸附到数据点
        self._cross_v.set_xdata([cx, cx])
        self._cross_h.set_ydata([cy, cy])
        # 红圆点标记最近数据点（吸附）
        idx = self._nearest_index(cx)
        if idx is not None:
            px = self._chart_xs_nums[idx]
            py = self._chart_mids[idx]
            pdate = self._chart_xs[idx]
            if self._chart_dot is None:
                self._chart_dot = self.ax.scatter(
                    [px], [py], s=55, color="#c33", zorder=12,
                    edgecolors="white", linewidths=1.2,
                )
            else:
                self._chart_dot.set_offsets([[px, py]])
                self._chart_dot.set_visible(True)
            extra = ""
            if self._chart_buy[idx] is not None:
                extra += f"\n现汇买入 {self._chart_buy[idx]:.4f}"
            if self._chart_sell[idx] is not None:
                extra += f"\n现汇卖出 {self._chart_sell[idx]:.4f}"
            self._cross_text.xy = (cx, cy)
            self._cross_text.set_text(
                f"{pdate:%Y-%m-%d %H:%M}\n中行折算 {py:.4f}{extra}"
            )
            self._cross_text.set_visible(True)
        self._cross_v.set_visible(True)
        self._cross_h.set_visible(True)
        self.canvas.draw_idle()

    def _on_axes_leave(self, _event):
        self._hide_crosshair()

    def _on_scroll(self, event):
        if event.inaxes != self.ax or event.xdata is None or event.ydata is None:
            return
        factor = 1 / 1.25 if event.button == "up" else 1.25
        x, y = event.xdata, event.ydata
        xl = self.ax.get_xlim()
        yl = self.ax.get_ylim()
        self.ax.set_xlim(x - (x - xl[0]) * factor, x + (xl[1] - x) * factor)
        self.ax.set_ylim(y - (y - yl[0]) * factor, y + (yl[1] - y) * factor)
        self.canvas.draw_idle()

    def _on_press(self, event):
        if event.inaxes != self.ax or event.button != 1:
            return
        self._pan_origin = {
            "xlim": self.ax.get_xlim(),
            "ylim": self.ax.get_ylim(),
            "x_pix": event.x,
            "y_pix": event.y,
        }
        self._pan_moved = False
        try:
            self.canvas.get_tk_widget().config(cursor="fleur")
        except Exception:
            pass

    def _on_release(self, event):
        if self._pan_origin is None:
            return
        moved = self._pan_moved
        self._pan_origin = None
        self._pan_moved = False
        try:
            self.canvas.get_tk_widget().config(cursor="")
        except Exception:
            pass
        if moved:
            return
        # 没拖动 → 当作点击：锁定标记 + 复制
        if event.inaxes != self.ax or event.button != 1 or event.xdata is None:
            return
        idx = self._nearest_index(event.xdata)
        if idx is None:
            return
        pdate = self._chart_xs[idx]
        py = self._chart_mids[idx]
        if self._click_marker is not None:
            try: self._click_marker.remove()
            except Exception: pass
        if self._click_text is not None:
            try: self._click_text.remove()
            except Exception: pass
        self._click_marker = self.ax.scatter(
            [self._chart_xs_nums[idx]], [py], s=90, facecolors="none",
            edgecolors="#c33", linewidths=1.8, zorder=15,
        )
        extra = ""
        if self._chart_buy[idx] is not None:
            extra += f"\n买 {self._chart_buy[idx]:.4f}"
        if self._chart_sell[idx] is not None:
            extra += f"\n卖 {self._chart_sell[idx]:.4f}"
        self._click_text = self.ax.annotate(
            f"{pdate:%Y-%m-%d %H:%M}\n折算 {py:.4f}{extra}",
            xy=(self._chart_xs_nums[idx], py),
            xytext=(14, -28), textcoords="offset points",
            bbox=dict(boxstyle="round,pad=0.4", fc="#fff", ec="#c33", lw=1.2),
            fontsize=9, zorder=20,
        )
        copy_text = f"{pdate:%Y-%m-%d %H:%M:%S}  {py:.4f}"
        self.root.clipboard_clear()
        self.root.clipboard_append(copy_text)
        self.status_var.set(f"已复制到剪贴板：{copy_text}")
        self.canvas.draw_idle()

    def on_close(self):
        self.stop_event.set()
        try:
            self.conn.close()
        except Exception:
            pass
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
