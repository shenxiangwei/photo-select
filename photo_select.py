"""
照片选择工具 (Photo Select)
============================

在 Windows 上从一堆照片中挑选最终保留的照片。

核心操作（键盘）:
    ← / →        上一张 / 下一张
    空格 / Y     标记为「保留」
    X / Delete   标记为「删除」
    U            取消标记
    Home / End   跳到第一张 / 最后一张
    Ctrl+A       全选为保留
    Ctrl+Shift+A 取消所有标记
    Ctrl+O       打开文件夹
    Ctrl+E       导出（把标记为保留的照片复制到输出文件夹）
    Ctrl+Z       撤销上一次标记
    F11          全屏
    Esc          退出全屏
    F1           显示帮助

标记后自动跳到下一张（可在菜单里关闭）。
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import tkinter as tk
from pathlib import Path
from queue import Queue, Empty
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from PIL import Image, ImageOps, ImageTk, ImageFile

# 允许 Pillow 读取截断的图片
ImageFile.LOAD_TRUNCATED_IMAGES = True


def _detect_dpi_scale() -> float:
    """在 Windows 上检测 DPI 缩放比例（1.0 = 96 DPI），并让进程 DPI-aware。

    必须在创建 Tk root 之前调用，否则 Windows 会做虚拟缩放导致模糊或字体过小。
    """
    try:
        import ctypes
        # 让进程成为 per-monitor DPI aware，拿到真实 DPI
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass
        hdc = ctypes.windll.user32.GetDC(0)
        try:
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        finally:
            ctypes.windll.user32.ReleaseDC(0, hdc)
        if dpi and dpi > 0:
            return max(1.0, dpi / 96.0)
    except Exception:
        pass
    return 1.0


DPI_SCALE = _detect_dpi_scale()

# 字体整体放大系数。tk scaling = DPI_SCALE * FONT_BOOST：
#   1.0 = 物理英寸正确（11pt 视觉等同 96dpi 下 11px，偏小）
#   1.25 = 比 1.0 大 25%（推荐，长时间看图更舒适）
#   1.5  = 明显偏大
# 觉得字还小就调大、觉得大就调小，只改这一个数。
FONT_BOOST = 1.25


def F(size: int) -> int:
    """字号 helper。整体缩放由 tk scaling 统一处理，这里返回原值。

    保留函数是为了将来手动微调方便（例如某个元素想比基准大一号）。
    """
    return max(8, size)


# 支持的图片扩展名
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".tiff", ".tif", ".jfif"}

# 标记状态
KEEP = "keep"
DROP = "drop"
NONE = "none"

# 状态对应的中文与颜色
STATE_TEXT = {KEEP: "保留", DROP: "删除", NONE: "未标记"}
STATE_COLOR = {
    KEEP: "#2e7d32",   # 绿
    DROP: "#c62828",   # 红
    NONE: "#9e9e9e",   # 灰
}
# 缩略图边框色（用更亮的色，便于在深色背景上识别）
STATE_BORDER = {
    KEEP: "#4caf50",   # 亮绿
    DROP: "#ef5350",   # 亮红
    NONE: "#5a5a5a",   # 暗灰
}
# 当前选中照片的发光色
CURRENT_GLOW = "#FFD600"   # 亮黄
CURRENT_GLOW_SOFT = "#FFE082"

# 缩略图条：滑动窗口式，只显示当前附近 THUMB_WINDOW 个槽位
THUMB_SIZE = 130          # 单个缩略图边长
THUMB_WINDOW = 20         # 一共显示多少个槽位
THUMB_GAP = 10            # 槽位间距
THUMB_BAR_H = THUMB_SIZE + 28   # 缩略图条高度


class ThumbCache:
    """后台生成缩略图，避免 UI 卡顿。"""

    def __init__(self, photo_select: "PhotoSelect"):
        self.app = photo_select
        self.queue: Queue = Queue()
        self.results: Queue = Queue()
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def request(self, index: int, path: Path, size: tuple[int, int]):
        self.queue.put((index, str(path), size))

    def _worker(self):
        while True:
            try:
                item = self.queue.get(timeout=0.5)
            except Empty:
                continue
            if item is None:
                break
            index, path_str, size = item
            try:
                with Image.open(path_str) as im:
                    im = ImageOps.exif_transpose(im)
                    im = im.convert("RGB")
                    im.thumbnail(size, Image.LANCZOS)
                    self.results.put((index, path_str, im))
            except Exception:
                self.results.put((index, path_str, None))
            # 让主线程处理结果
            self.app.root.after(0, self.app.drain_thumb_results)


class ImageLoader:
    """后台解码大图（带 EXIF 朝向修正）+ 预加载缓存。

    缓存最近若干张已解码的显示尺寸 PIL Image，主线程取到后才转
    PhotoImage 显示，避免 U盘 / 网盘等慢盘导致 UI 卡死。
    """

    def __init__(self, app: "PhotoSelect", keep_n: int = 6):
        self.app = app
        self.keep_n = keep_n
        self.queue: Queue = Queue()
        self.results: Queue = Queue()
        self.cache: dict[int, Image.Image] = {}
        self.errors: dict[int, str] = {}
        self.pending: set[int] = set()
        self.gen = 0  # 失效计数，旧 generation 的结果会被丢弃
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def request(self, index: int, size: tuple[int, int], fit: str):
        if index in self.cache or index in self.errors or index in self.pending:
            return
        if not (0 <= index < len(self.app.photos)):
            return
        self.pending.add(index)
        self.queue.put((self.gen, index, str(self.app.photos[index]),
                       size, fit))

    def invalidate(self):
        """目录或窗口尺寸变化时清空缓存。"""
        self.gen += 1
        self.cache.clear()
        self.errors.clear()
        self.pending.clear()
        # 清空请求队列
        while True:
            try:
                self.queue.get_nowait()
            except Empty:
                break

    def trim(self, around: int):
        """保留 around 附近 keep_n 张，其余释放。"""
        if len(self.cache) <= self.keep_n:
            return
        keep = set(range(max(0, around - self.keep_n // 2),
                         around + self.keep_n // 2 + 1))
        for k in list(self.cache.keys()):
            if k not in keep:
                self.cache.pop(k, None)

    def _worker(self):
        while True:
            try:
                item = self.queue.get(timeout=0.5)
            except Empty:
                continue
            if item is None:
                break
            gen, index, path_str, size, fit = item
            try:
                with Image.open(path_str) as im:
                    im = ImageOps.exif_transpose(im)
                    im.load()
                cw, ch = size
                if fit == "fit":
                    disp = im.copy()
                    disp.thumbnail((cw, ch), Image.LANCZOS)
                    if disp.mode in ("RGBA", "P", "LA"):
                        bg = Image.new("RGB", disp.size, (30, 30, 30))
                        if disp.mode == "P":
                            disp = disp.convert("RGBA")
                        bg.paste(disp, mask=disp.split()[-1]
                                 if disp.mode == "RGBA" else None)
                        disp = bg
                    else:
                        disp = disp.convert("RGB")
                else:
                    disp = im.convert("RGB")
                self.results.put((gen, index, "ok", disp))
            except Exception as e:
                self.results.put((gen, index, "err", str(e)))
            self.app.root.after(0, self.app.drain_load_results)


class ThumbItem:
    """缩略图条里的一个槽位。槽位对应的 photo index 可变（滑动窗口）。

    index == -1 表示空槽位（窗口边界外）。
    """

    def __init__(self, app: "PhotoSelect", x: int, y: int):
        self.app = app
        self.canvas = app.thumb_canvas
        self.index = -1
        self.size = THUMB_SIZE
        self.x = x
        self.y = y
        self.image_ref: Optional[ImageTk.PhotoImage] = None
        canvas = self.canvas
        s = self.size
        pad = 4
        # 外发光晕（最底层，仅当前显示）
        self.glow_outer = canvas.create_rectangle(
            x - pad - 4, y - pad - 4, x + s + pad + 4, y + s + pad + 4,
            outline="", width=0, tags=("thumb",)
        )
        self.glow_inner = canvas.create_rectangle(
            x - pad - 2, y - pad - 2, x + s + pad + 2, y + s + pad + 2,
            outline="", width=0, tags=("thumb",)
        )
        # 主边框
        self.border = canvas.create_rectangle(
            x - pad, y - pad, x + s + pad, y + s + pad,
            outline=STATE_BORDER[NONE], width=2, tags=("thumb",)
        )
        # 图片（默认空，显示一个占位灰块）
        self.img_id = canvas.create_image(x, y, anchor="nw", tags=("thumb",))
        self.placeholder = canvas.create_rectangle(
            x, y, x + s, y + s, outline="", fill="#3a3a3a", tags=("thumb",)
        )
        # 右上角「当前」指示圆点
        dot_r = 9
        cx = x + s - dot_r
        cy = y + dot_r
        self.dot_ring = canvas.create_oval(
            cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r,
            outline="#212121", width=2, state="hidden", tags=("thumb",)
        )
        self.dot = canvas.create_oval(
            cx - dot_r + 2, cy - dot_r + 2, cx + dot_r - 2, cy + dot_r - 2,
            outline="", fill=CURRENT_GLOW, state="hidden", tags=("thumb",)
        )
        for item_id in (self.glow_outer, self.glow_inner, self.border,
                        self.img_id, self.placeholder):
            canvas.tag_bind(item_id, "<Button-1>", self._on_click)

    def set_index(self, index: int):
        self.index = index

    def set_image(self, photo: ImageTk.PhotoImage):
        self.image_ref = photo
        self.canvas.itemconfig(self.img_id, image=photo)
        # 有图了就隐藏占位灰块
        self.canvas.itemconfig(self.placeholder, state="hidden")

    def clear_image(self):
        """槽位变空或换 index 时清空图片。"""
        self.image_ref = None
        self.canvas.itemconfig(self.img_id, image="")
        self.canvas.itemconfig(self.placeholder, state="normal")

    def set_state(self, state: str, current: bool):
        """设置边框/发光/指示标记。

        - 非当前：边框 = 状态色（绿/红/灰），细
        - 当前：主边框亮黄粗 + 外面两层渐淡黄光晕 + 右上角黄点
                （canvas 的 stipple 只对 fill 生效、对 outline 无效，
                 所以用多层纯色同心矩形由外到内渐亮模拟发光）
        """
        c = self.canvas
        border_color = STATE_BORDER[state]
        if current:
            # 由外到内：淡黄粗 -> 亮黄细 -> 亮黄主边框
            c.itemconfig(self.glow_outer,
                         outline=CURRENT_GLOW_SOFT, width=5, state="normal")
            c.itemconfig(self.glow_inner,
                         outline=CURRENT_GLOW, width=3, state="normal")
            c.itemconfig(self.border, outline=CURRENT_GLOW, width=4)
            c.itemconfig(self.dot_ring, state="normal")
            c.itemconfig(self.dot, state="normal")
        else:
            c.itemconfig(self.border, outline=border_color, width=2)
            c.itemconfig(self.glow_outer, outline="", width=0, state="hidden")
            c.itemconfig(self.glow_inner, outline="", width=0, state="hidden")
            c.itemconfig(self.dot_ring, state="hidden")
            c.itemconfig(self.dot, state="hidden")

    def _on_click(self, _event=None):
        if self.index >= 0:
            self.app.goto(self.index)


class PhotoSelect:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("照片选择工具")
        root.geometry("1280x820")
        root.minsize(900, 600)

        # 数据
        self.photos: list[Path] = []
        self.current: int = 0
        self.states: dict[int, str] = {}   # index -> KEEP/DROP/NONE
        self.folder: Optional[Path] = None
        self.full_image: Optional[ImageTk.PhotoImage] = None
        self.thumb_items: list[ThumbItem] = []
        self.thumb_loaded: set[int] = set()      # 已请求解码的 photo index
        self.thumb_images: dict[int, ImageTk.PhotoImage] = {}  # index -> 图
        self.history: list[tuple[int, str, str]] = []  # (index, old, new)
        self.auto_advance = tk.BooleanVar(value=True)
        self.fit_mode = tk.StringVar(value="fit")  # fit / actual
        self._fullscreen = False
        self._prev_geom = None
        # 当前显示尺寸的 token，窗口尺寸变化或切目录时递增，
        # 用来丢弃过期的预加载结果
        self._size_token = 0
        self._last_disp_size: tuple[int, int] = (0, 0)

        # 后台缩略图
        self.thumb_cache = ThumbCache(self)
        # 后台大图解码 / 预加载
        self.image_loader = ImageLoader(self)

        self._build_ui()
        self._bind_keys()
        self.root.after(100, self._poll_thumb_results)

        # 启动时若命令行给了文件夹就直接打开
        if len(sys.argv) > 1 and Path(sys.argv[1]).is_dir():
            self.open_folder(Path(sys.argv[1]))

    # ------------------ UI 构建 ------------------
    def _build_ui(self):
        # 顶部菜单
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="打开文件夹…  Ctrl+O",
                              command=self.cmd_open_folder)
        file_menu.add_command(label="导出保留的照片…  Ctrl+E",
                              command=self.cmd_export)
        file_menu.add_separator()
        file_menu.add_command(label="退出  Alt+F4", command=self.root.quit)
        menubar.add_cascade(label="文件", menu=file_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_checkbutton(label="标记后自动跳到下一张",
                                  variable=self.auto_advance)
        view_menu.add_separator()
        view_menu.add_radiobutton(label="适应窗口", variable=self.fit_mode,
                                  value="fit", command=self._reload_current)
        view_menu.add_radiobutton(label="原始大小 (1:1)",
                                  variable=self.fit_mode, value="actual",
                                  command=self._reload_current)
        view_menu.add_separator()
        view_menu.add_command(label="全屏  F11", command=self.toggle_fullscreen)
        menubar.add_cascade(label="视图", menu=view_menu)

        edit_menu = tk.Menu(menubar, tearoff=0)
        edit_menu.add_command(label="撤销标记  Ctrl+Z",
                              command=self.cmd_undo)
        edit_menu.add_separator()
        edit_menu.add_command(label="全部标记为保留  Ctrl+A",
                              command=self.cmd_mark_all_keep)
        edit_menu.add_command(label="取消所有标记  Ctrl+Shift+A",
                              command=self.cmd_clear_all)
        menubar.add_cascade(label="编辑", menu=edit_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="快捷键…  F1", command=self.show_help)
        menubar.add_cascade(label="帮助", menu=help_menu)
        self.root.config(menu=menubar)

        # 顶部状态栏
        top = ttk.Frame(self.root, padding=(8, 4))
        top.pack(side="top", fill="x")
        self.lbl_status = ttk.Label(top, text="未打开文件夹", font=("Segoe UI", F(11)))
        self.lbl_status.pack(side="left")
        # 进度条：缩略图加载进度（U盘大文件夹时可见）
        self.progress = ttk.Progressbar(
            top, length=240, mode="determinate",
            maximum=100, value=0
        )
        self.progress.pack(side="left", padx=(16, 0), pady=2)
        self.lbl_progress = ttk.Label(top, text="", font=("Segoe UI", F(10)))
        self.lbl_progress.pack(side="left", padx=(6, 0))
        self.lbl_state = tk.Label(top, text="", font=("Segoe UI", F(12), "bold"))
        self.lbl_state.pack(side="right")

        # 中间图片显示区
        self.canvas = tk.Canvas(self.root, bg="#1e1e1e", highlightthickness=0)
        self.canvas.pack(side="top", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._reload_current())

        # 底部缩略图条（滑动窗口式：只显示当前附近 THUMB_WINDOW 个槽位）
        bottom = ttk.Frame(self.root)
        bottom.pack(side="bottom", fill="x")
        self.thumb_outer = ttk.Frame(bottom)
        self.thumb_outer.pack(side="top", fill="x")
        self.thumb_canvas = tk.Canvas(self.thumb_outer,
                                      height=THUMB_BAR_H,
                                      highlightthickness=0, bg="#2b2b2b")
        self.thumb_canvas.pack(side="top", fill="x", expand=True)
        self.thumb_canvas.bind("<Configure>",
                               lambda e: self._layout_thumbs())

        # 底部提示
        hint = ttk.Frame(bottom, padding=(8, 4))
        hint.pack(side="bottom", fill="x")
        ttk.Label(hint,
                  text="←/→ 切换    空格/Y 保留    X/Delete 删除    U 取消    Ctrl+E 导出    F1 帮助",
                  foreground="#666").pack(side="left")

        # 空状态提示
        self._draw_empty_hint()

    def _draw_empty_hint(self):
        self.canvas.delete("all")
        self.canvas.create_text(
            self.canvas.winfo_width() // 2 or 400,
            self.canvas.winfo_height() // 2 or 300,
            text="按 Ctrl+O 打开照片文件夹",
            fill="#888", font=("Segoe UI", F(18))
        )

    # ------------------ 键盘绑定 ------------------
    def _bind_keys(self):
        self.root.bind("<Left>", lambda e: self.goto(self.current - 1))
        self.root.bind("<Right>", lambda e: self.goto(self.current + 1))
        self.root.bind("<space>", lambda e: self.mark(KEEP))
        self.root.bind("y", lambda e: self.mark(KEEP))
        self.root.bind("Y", lambda e: self.mark(KEEP))
        self.root.bind("x", lambda e: self.mark(DROP))
        self.root.bind("X", lambda e: self.mark(DROP))
        self.root.bind("<Delete>", lambda e: self.mark(DROP))
        self.root.bind("u", lambda e: self.mark(NONE))
        self.root.bind("U", lambda e: self.mark(NONE))
        self.root.bind("<Home>", lambda e: self.goto(0))
        self.root.bind("<End>", lambda e: self.goto(len(self.photos) - 1))
        self.root.bind("<Control-o>", lambda e: self.cmd_open_folder())
        self.root.bind("<Control-O>", lambda e: self.cmd_open_folder())
        self.root.bind("<Control-e>", lambda e: self.cmd_export())
        self.root.bind("<Control-E>", lambda e: self.cmd_export())
        self.root.bind("<Control-z>", lambda e: self.cmd_undo())
        self.root.bind("<Control-Z>", lambda e: self.cmd_undo())
        self.root.bind("<Control-a>", lambda e: self.cmd_mark_all_keep())
        self.root.bind("<Control-A>", lambda e: self.cmd_mark_all_keep())
        self.root.bind("<Control-Shift-A>",
                       lambda e: self.cmd_clear_all())
        self.root.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.root.bind("<Escape>", lambda e: self._on_escape())
        self.root.bind("<F1>", lambda e: self.show_help())
        # 鼠标滚轮翻页
        self.canvas.bind("<MouseWheel>",
                         lambda e: self.goto(self.current + (1 if e.delta < 0 else -1)))

    # ------------------ 文件夹 ------------------
    def cmd_open_folder(self):
        path = filedialog.askdirectory(title="选择照片文件夹")
        if path:
            self.open_folder(Path(path))

    def open_folder(self, folder: Path):
        self.folder = folder
        files = [p for p in folder.iterdir()
                 if p.is_file()
                 and p.suffix.lower() in IMAGE_EXTS
                 and not p.name.startswith(".")]
        files.sort(key=lambda p: p.name.lower())
        if not files:
            messagebox.showinfo("提示", "该文件夹中没有图片文件。")
            return
        self.photos = files
        self.current = 0
        self.states.clear()
        self.history.clear()
        self.thumb_loaded.clear()
        self.thumb_images.clear()
        self.image_loader.invalidate()
        self._build_thumbs()
        self._reload_current()
        self._update_thumb_window()
        self.root.title(f"照片选择工具 - {folder}")

    # ------------------ 导航 ------------------
    def goto(self, index: int):
        if not self.photos:
            return
        index = max(0, min(len(self.photos) - 1, index))
        if index == self.current:
            return
        self.current = index
        self._reload_current()
        self._update_thumb_window()

    def _reload_current(self):
        self.canvas.delete("all")
        if not self.photos:
            self._draw_empty_hint()
            self._update_status()
            return
        cw = max(self.canvas.winfo_width(), 100)
        ch = max(self.canvas.winfo_height(), 100)
        # 尺寸或显示模式变化时，旧缓存作废
        fit = self.fit_mode.get()
        size_changed = (cw, ch) != self._last_disp_size
        if size_changed or fit != getattr(self, "_last_fit", None):
            self.image_loader.invalidate()
            self._last_disp_size = (cw, ch)
            self._last_fit = fit
        idx = self.current

        # 缓存命中：直接显示
        if idx in self.image_loader.cache:
            self._display_image(idx, cw, ch)
            self._schedule_prefetch(idx, cw, ch, fit)
            self._update_status()
            return
        if idx in self.image_loader.errors:
            self.canvas.create_text(
                cw // 2, ch // 2,
                text=f"无法打开图片\n{self.photos[idx].name}\n"
                     f"{self.image_loader.errors[idx]}",
                fill="#e53935", font=("Segoe UI", F(14))
            )
            self._update_status()
            return

        # 未命中：显示「加载中」，请求解码
        self.canvas.create_text(
            cw // 2, ch // 2,
            text="加载中…", fill="#bbb", font=("Segoe UI", F(16))
        )
        self._update_status()
        self.image_loader.request(idx, (cw, ch), fit)
        # 同时预加载下一张
        self._schedule_prefetch(idx, cw, ch, fit)

    def _display_image(self, idx: int, cw: int, ch: int):
        """把缓存里的 PIL Image 转成 PhotoImage 显示到画布。"""
        disp = self.image_loader.cache.get(idx)
        if disp is None:
            return
        self.full_image = ImageTk.PhotoImage(disp)
        x = (cw - disp.width) // 2
        y = (ch - disp.height) // 2
        self.canvas.create_image(x, y, anchor="nw", image=self.full_image)

    def _schedule_prefetch(self, idx: int, cw: int, ch: int, fit: str):
        """预加载当前 +1/+2 和 -1，让连续翻页流畅。"""
        total = len(self.photos)
        for delta in (1, 2, -1):
            n = idx + delta
            if 0 <= n < total and n not in self.image_loader.cache:
                self.image_loader.request(n, (cw, ch), fit)

    def drain_load_results(self):
        """主线程：处理后台大图解码结果。"""
        loader = self.image_loader
        try:
            while True:
                item = loader.results.get_nowait()
                gen, index, status, payload = item
                # 丢弃过期的结果（目录/尺寸已变）
                if gen != loader.gen:
                    continue
                if not (0 <= index < len(self.photos)):
                    continue
                if status == "ok":
                    loader.cache[index] = payload
                    loader.pending.discard(index)
                    # 如果正是当前要显示的，刷新一下
                    if index == self.current:
                        self._reload_current()
                else:
                    loader.errors[index] = payload
                    loader.pending.discard(index)
                    if index == self.current:
                        self._reload_current()
        except Empty:
            pass
        # 释放离当前太远的缓存
        loader.trim(self.current)

    def _update_status(self):
        if not self.photos:
            self.lbl_status.config(text="未打开文件夹")
            self.lbl_state.config(text="")
            return
        total = len(self.photos)
        keep = sum(1 for s in self.states.values() if s == KEEP)
        drop = sum(1 for s in self.states.values() if s == DROP)
        name = self.photos[self.current].name
        # 进度提示：当前未加载完成时显示「加载中」
        loading = ""
        if self.current in self.image_loader.pending:
            loading = "    [加载中…]"
        self.lbl_status.config(
            text=f"{self.current + 1} / {total}    {name}    "
                 f"保留 {keep}    删除 {drop}{loading}"
        )
        state = self.states.get(self.current, NONE)
        self.lbl_state.config(
            text=f"● {STATE_TEXT[state]}",
            fg=STATE_COLOR[state],
        )
        self._update_thumb_states()

    # ------------------ 标记 ------------------
    def mark(self, state: str):
        if not self.photos:
            return
        old = self.states.get(self.current, NONE)
        self.history.append((self.current, old, state))
        self.states[self.current] = state
        self._update_status()
        if self.auto_advance.get() and state != NONE:
            self.goto(self.current + 1)

    def cmd_undo(self):
        if not self.history:
            return
        idx, old, _new = self.history.pop()
        if old == NONE:
            self.states.pop(idx, None)
        else:
            self.states[idx] = old
        self.current = idx
        self._reload_current()
        self._update_thumb_window()
        self._update_status()

    def cmd_mark_all_keep(self):
        for i in range(len(self.photos)):
            self.states[i] = KEEP
        self._update_status()

    def cmd_clear_all(self):
        self.states.clear()
        self.history.clear()
        self._update_status()

    # ------------------ 导出 ------------------
    def cmd_export(self):
        if not self.photos:
            return
        keep_idx = [i for i in range(len(self.photos))
                    if self.states.get(i) == KEEP]
        if not keep_idx:
            if messagebox.askyesno(
                "提示",
                "没有标记为「保留」的照片。是否标记全部为保留后导出？"
            ):
                self.cmd_mark_all_keep()
                keep_idx = list(range(len(self.photos)))
            else:
                return
        # 选择输出文件夹
        out = filedialog.askdirectory(title="选择导出目标文件夹")
        if not out:
            return
        out_dir = Path(out)
        out_dir.mkdir(parents=True, exist_ok=True)
        self._run_export(keep_idx, out_dir)

    def _run_export(self, keep_idx: list[int], out_dir: Path):
        """后台复制 + 进度对话框 + 可取消。"""
        import queue as _queue
        q: _queue.Queue = _queue.Queue()
        cancel = {"flag": False}

        win = tk.Toplevel(self.root)
        win.title("导出中")
        win.geometry("480x160")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        f = ttk.Frame(win, padding=18)
        f.pack(fill="both", expand=True)
        lbl = ttk.Label(f, text=f"准备导出 {len(keep_idx)} 张照片…",
                       font=("Segoe UI", F(11)))
        lbl.pack(anchor="w")
        bar = ttk.Progressbar(f, length=440, mode="determinate",
                              maximum=max(1, len(keep_idx)), value=0)
        bar.pack(fill="x", pady=(10, 6))
        sub = ttk.Label(f, text="", font=("Segoe UI", F(10)),
                        foreground="#666")
        sub.pack(anchor="w")
        btn_cancel = ttk.Button(f, text="取消",
                                command=lambda: cancel.update(flag=True))
        btn_cancel.pack(side="right", pady=(10, 0))

        def worker():
            copied = 0
            errors: list[str] = []
            for n, i in enumerate(keep_idx):
                if cancel["flag"]:
                    break
                src = self.photos[i]
                dst = out_dir / src.name
                if dst.exists():
                    stem, ext = dst.stem, dst.suffix
                    k = 1
                    while True:
                        cand = out_dir / f"{stem}_{k}{ext}"
                        if not cand.exists():
                            dst = cand
                            break
                        k += 1
                q.put(("progress", n + 1, src.name))
                try:
                    shutil.copy2(src, dst)
                    copied += 1
                except Exception as e:
                    errors.append(f"{src.name}: {e}")
            q.put(("done", copied, errors, cancel["flag"]))

        threading.Thread(target=worker, daemon=True).start()

        def poll():
            try:
                while True:
                    kind, *rest = q.get_nowait()
                    if kind == "progress":
                        n, name = rest
                        bar.config(value=n)
                        lbl.config(text=f"导出中 {n} / {len(keep_idx)}")
                        sub.config(text=name)
                    elif kind == "done":
                        copied, errors, was_cancel = rest
                        win.destroy()
                        if was_cancel:
                            title = "导出已取消"
                        else:
                            title = "导出完成"
                        msg = (f"已复制 {copied} 张照片到:\n{out_dir}")
                        if errors:
                            msg += "\n\n失败:\n" + "\n".join(errors[:10])
                            if len(errors) > 10:
                                msg += f"\n…还有 {len(errors) - 10} 个"
                        messagebox.showinfo(title, msg)
                        return
            except Empty:
                pass
            self.root.after(80, poll)

        self.root.after(80, poll)

    # ------------------ 缩略图条（滑动窗口）------------------
    def _build_thumbs(self):
        # 清旧槽位
        for item in self.thumb_items:
            for cid in (item.glow_outer, item.glow_inner, item.border,
                        item.img_id, item.placeholder, item.dot_ring,
                        item.dot):
                self.thumb_canvas.delete(cid)
        self.thumb_items = []
        # 创建 THUMB_WINDOW 个空槽位，一次性布局好
        step = THUMB_SIZE + THUMB_GAP
        y = max(6, (THUMB_BAR_H - THUMB_SIZE) // 2)
        for i in range(THUMB_WINDOW):
            x = 8 + i * step
            item = ThumbItem(self, x, y)
            self.thumb_items.append(item)
        total_w = 8 + THUMB_WINDOW * step + 8
        self.thumb_canvas.config(scrollregion=(0, 0, total_w, THUMB_BAR_H))
        self._center_thumb_view()

    def _layout_thumbs(self):
        """窗口尺寸变化时重新居中（槽位布局是固定的）。"""
        if self.thumb_items:
            self._center_thumb_view()

    def _center_thumb_view(self):
        """让中间槽位（对应 current）显示在画布中间。"""
        if not self.thumb_items:
            return
        view_w = self.thumb_canvas.winfo_width()
        if view_w <= 1:
            self.root.after(50, self._center_thumb_view)
            return
        step = THUMB_SIZE + THUMB_GAP
        mid_slot = THUMB_WINDOW // 2
        mid_center = 8 + mid_slot * step + THUMB_SIZE // 2
        total_w = 8 + THUMB_WINDOW * step + 8
        target = mid_center - view_w // 2
        max_scroll = max(0, total_w - view_w)
        target = max(0, min(max_scroll, target))
        self.thumb_canvas.xview_moveto(target / max(1, total_w))

    def _update_thumb_window(self):
        """根据 current 刷新所有槽位的 photo index、图片、状态。"""
        if not self.thumb_items:
            return
        window_start = self.current - THUMB_WINDOW // 2
        size = THUMB_SIZE
        for i, slot in enumerate(self.thumb_items):
            photo_idx = window_start + i
            if 0 <= photo_idx < len(self.photos):
                slot.set_index(photo_idx)
                img = self.thumb_images.get(photo_idx)
                if img is not None:
                    slot.set_image(img)
                else:
                    slot.clear_image()
                    # 请求解码（已请求过的不再重复入队）
                    if photo_idx not in self.thumb_loaded:
                        self.thumb_loaded.add(photo_idx)
                        self.thumb_cache.request(
                            photo_idx, self.photos[photo_idx], (size, size)
                        )
            else:
                slot.set_index(-1)
                slot.clear_image()
        self._update_thumb_states()
        self._center_thumb_view()
        self._update_progress()

    def _update_thumb_states(self):
        for slot in self.thumb_items:
            if slot.index < 0:
                slot.set_state(NONE, current=False)
                continue
            state = self.states.get(slot.index, NONE)
            slot.set_state(state, current=(slot.index == self.current))

    def _poll_thumb_results(self):
        self.drain_thumb_results()
        self.root.after(80, self._poll_thumb_results)

    def drain_thumb_results(self):
        """主线程：把后台缩略图转成 PhotoImage 存缓存，并刷新窗口内槽位。"""
        try:
            while True:
                index, _path, pil_im = self.thumb_cache.results.get_nowait()
                if pil_im is not None and 0 <= index < len(self.photos):
                    photo = ImageTk.PhotoImage(pil_im)
                    self.thumb_images[index] = photo
                    # 找到当前窗口内对应槽位并刷新
                    for slot in self.thumb_items:
                        if slot.index == index:
                            slot.set_image(photo)
                            break
        except Empty:
            pass
        self._update_progress()

    def _update_progress(self):
        """进度条：当前窗口内已加载缩略图的比例。"""
        if not self.photos or not self.thumb_items:
            self.progress.config(maximum=100, value=0)
            self.lbl_progress.config(text="")
            return
        window = [s.index for s in self.thumb_items if s.index >= 0]
        total = len(window)
        loaded = sum(1 for idx in window if idx in self.thumb_images)
        self.progress.config(maximum=max(1, total), value=loaded)
        if total == 0:
            self.lbl_progress.config(text="")
        elif loaded >= total:
            self.lbl_progress.config(text=f"缩略图 {loaded}/{total} ✓")
        else:
            pct = int(round(loaded / total * 100)) if total else 0
            self.lbl_progress.config(
                text=f"缩略图 {loaded}/{total} ({pct}%)"
            )

    # ------------------ 全屏 / 帮助 / Esc ------------------
    def toggle_fullscreen(self):
        self._fullscreen = not self._fullscreen
        if self._fullscreen:
            self._prev_geom = self.root.geometry()
            self.root.attributes("-fullscreen", True)
        else:
            self.root.attributes("-fullscreen", False)
            if self._prev_geom:
                self.root.geometry(self._prev_geom)

    def _on_escape(self):
        if self._fullscreen:
            self.toggle_fullscreen()

    def show_help(self):
        text = (
            "快捷键\n"
            "──────────────────────\n"
            "← / →              上一张 / 下一张\n"
            "空格 / Y           标记为「保留」\n"
            "X / Delete         标记为「删除」\n"
            "U                  取消标记\n"
            "Home / End         第一张 / 最后一张\n"
            "鼠标滚轮           翻页\n"
            "Ctrl+Z             撤销上一次标记\n"
            "Ctrl+A             全部标记为保留\n"
            "Ctrl+Shift+A       取消所有标记\n"
            "Ctrl+O             打开文件夹\n"
            "Ctrl+E             导出保留的照片\n"
            "F11                全屏\n"
            "Esc                退出全屏\n"
            "──────────────────────\n"
            "标记后默认自动跳到下一张，可在「视图」菜单关闭。"
        )
        win = tk.Toplevel(self.root)
        win.title("快捷键帮助")
        win.geometry("460x560")
        win.resizable(False, False)
        txt = tk.Text(win, font=("Consolas", F(11)), padx=16, pady=16,
                      wrap="word", bg="#fafafa", relief="flat")
        txt.insert("1.0", text)
        txt.config(state="disabled")
        txt.pack(fill="both", expand=True)
        win.transient(self.root)
        win.grab_set()


def _apply_named_fonts(root: tk.Tk):
    """按 DPI 缩放所有命名字体（菜单、ttk 默认控件等都会跟随）。"""
    from tkinter import font as tkfont
    family = "Segoe UI"
    for name, base in (("TkDefaultFont", 9), ("TkTextFont", 9),
                       ("TkMenuFont", 9), ("TkHeadingFont", 9),
                       ("TkCaptionFont", 10), ("TkSmallCaptionFont", 8),
                       ("TkTooltipFont", 8), ("TkIconFont", 10),
                       ("TkFixedFont", 9)):
        try:
            f = tkfont.nametofont(name)
            f.configure(family=family, size=F(base))
        except Exception:
            pass


def main():
    # DPI awareness 在模块 import 时已设置好（_detect_dpi_scale）。
    root = tk.Tk()
    # 让 tkinter 按 DPI 缩放点字号：scaling = DPI_SCALE * FONT_BOOST。
    # FONT_BOOST 在文件顶部，觉得字小/字大就改它一个数。
    try:
        root.tk.call("tk", "scaling", DPI_SCALE * FONT_BOOST)
    except Exception:
        pass
    _apply_named_fonts(root)
    PhotoSelect(root)
    root.mainloop()


if __name__ == "__main__":
    main()
