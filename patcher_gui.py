# -*- coding: utf-8 -*-
"""
nvidia_grid_driver_patcher GUI
================================
为 NVIDIA vGPU (GRID) 的 nvxdapix.dll 打 NLS 证书补丁。

流程:
  1. 扫描 DriverStore 中所有 nvxdapix.dll, 复选框多选要处理的版本
  2. 根证书: 文件选择 (读取内容到文本框) 或直接粘贴到文本框
  3. 打补丁前校验证书内容, 通过后写临时 .pem
  4. 对每个勾选的 DLL: 调用 gridd-unlock-patcher.exe 生成补丁,
     停服务 -> 备份 -> 授权 -> 覆盖 -> 起服务
"""

import os
import re
import sys
import base64
import ctypese
import subprocess
import tempfile
import psutil
import json
import shutil
import time
import atexit
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime

# ---------------------------------------------------------------- 路径处理
def resource_path(rel):
    """打包后资源位于 sys._MEIPASS, 开发时位于脚本目录。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def app_dir():
    """gridd-unlock-patcher.exe 所在目录 (与主程序同目录, 便于用户放补丁工具)。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def temp_dir():
    """程序同路径的 temp 文件夹, 用于存放待打补丁的 DLL 与证书副本。"""
    d = os.path.join(app_dir(), "temp")
    os.makedirs(d, exist_ok=True)
    return d


# gridd-unlock-patcher.exe 定位:
#   优先: 与主程序同目录 (app_dir), 发版时随 release 一起提供
#   后备: 打包内置 (sys._MEIPASS, 由 --add-data 打入)
def find_patcher_exe():
    cands = []
    cands.append(os.path.join(app_dir(), "gridd-unlock-patcher.exe"))
    if getattr(sys, "_MEIPASS", None):
        cands.append(os.path.join(sys._MEIPASS, "gridd-unlock-patcher.exe"))
    for c in cands:
        if os.path.exists(c):
            return c
    # 都不存在时, 返回首选路径(同目录), 供启动检查报错使用
    return cands[0]


PATCHER_EXE = find_patcher_exe()

def find_processes_using_dll(path):
    """返回加载了指定 DLL 的进程列表 [(pid, 进程名), ...]。

    用 psutil 遍历所有进程的已映射模块, 精确匹配 DLL 路径 (不区分大小写)。
    """
    low = os.path.normcase(path)
    hit = []
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            for m in proc.memory_maps():
                if m.path and os.path.normcase(m.path) == low:
                    hit.append((proc.info['pid'], proc.info['name']))
                    break
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return hit


def kill_processes_using_dll(path, log=None):
    """强杀所有仍加载指定 DLL 的进程 (停服务后的兜底, 对齐 ps1 的 Get-Process 逻辑)。

    返回被强杀的进程数。
    """
    killed = 0
    for pid, name in find_processes_using_dll(path):
        try:
            prc = psutil.Process(pid)
            prc.terminate()
            if log:
                log("  正在结束占用进程: %s (PID %d)" % (name, pid))
            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    # 给被 terminate 的进程一点时间退出
    if killed:
        gone, alive = psutil.wait_procs(
            [psutil.Process(pid) for pid, _ in find_processes_using_dll(path)],
            timeout=5,
        )
        for prc in alive:
            try:
                prc.kill()
                if log:
                    log("  强制结束进程: %s (PID %d)" % (prc.name(), prc.pid))
            except Exception:
                pass
    return killed


def backup_session_dir():
    """创建一次备份会话目录: BACKUP_DIR/<unix时间戳>/, 返回该目录。"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = int(time.time())
    d = os.path.join(BACKUP_DIR, str(ts))
    os.makedirs(d, exist_ok=True)
    return d, ts


def backup_one_dll(dll_path, session_dir, log=None):
    """备份单个 DLL 到会话目录, 保留 DriverStore 下的相对路径结构。

    例: C:/Windows/.../DriverStore/FileRepository/nv_dispsi.inf_amd64_x/nvxdapix.dll
        -> <session_dir>/FileRepository/nv_dispsi.inf_amd64_x/nvxdapix.dll
    返回该 DLL 在会话目录内的相对路径 (相对 DriverStore), 失败返回 None。
    """
    try:
        # 相对 DriverStore 的路径 (保留 FileRepository\xxx 结构, 保留原始大小写)
        drv_norm = os.path.normcase(DRV_ROOT)
        full_norm = os.path.normcase(dll_path)
        if full_norm.startswith(drv_norm):
            rel = os.path.relpath(dll_path, DRV_ROOT)
        else:
            # 不在 DriverStore 下时, 用父目录名 + 文件名兜底
            rel = os.path.basename(os.path.dirname(dll_path)) + "\\" + os.path.basename(dll_path)
        dest = os.path.join(session_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(dll_path, dest)
        if log:
            log("  已备份: %s -> %s" % (dll_path, dest))
        return rel
    except Exception as e:
        if log:
            log("  [错误] 备份失败 %s: %s" % (dll_path, e))
        return None


def write_manifest(session_dir, entries):
    """写 manifest.json, entries: [(原始dll路径, 会话内相对路径), ...]"""
    data = {
        "created_ts": int(time.time()),
        "created_str": datetime.fromtimestamp(int(time.time())).strftime("%Y-%m-%d %H:%M:%S"),
        "items": [{"original": o, "rel": r} for o, r in entries],
    }
    mp = os.path.join(session_dir, "manifest.json")
    with open(mp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return mp


def list_backup_sessions():
    """列出 BACKUP_DIR 下所有有效会话目录 (含 manifest.json)。
    返回 [(会话目录, 时间戳int, 创建时间str), ...] 按时间倒序。"""
    result = []
    if not os.path.isdir(BACKUP_DIR):
        return result
    for name in os.listdir(BACKUP_DIR):
        d = os.path.join(BACKUP_DIR, name)
        if not os.path.isdir(d) or not name.isdigit():
            continue
        mp = os.path.join(d, "manifest.json")
        if not os.path.exists(mp):
            continue
        try:
            with open(mp, "r", encoding="utf-8") as f:
                meta = json.load(f)
            cstr = meta.get("created_str", datetime.fromtimestamp(int(name)).strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            cstr = datetime.fromtimestamp(int(name)).strftime("%Y-%m-%d %H:%M:%S")
        result.append((d, int(name), cstr))
    result.sort(key=lambda x: x[1], reverse=True)
    return result


def read_manifest(session_dir):
    """读 manifest.json, 返回 [(原始dll路径, 会话内相对路径), ...], 失败返回 []。"""
    mp = os.path.join(session_dir, "manifest.json")
    if not os.path.exists(mp):
        return []
    try:
        with open(mp, "r", encoding="utf-8") as f:
            meta = json.load(f)
        return [(it["original"], it["rel"]) for it in meta.get("items", [])]
    except Exception:
        return []


DRV_ROOT = r"C:\Windows\System32\DriverStore\FileRepository"
SVC_NAME = "NVDisplay.ContainerLocalSystem"
BACKUP_DIR = os.path.join(app_dir(), "backup")

BEGIN_MARK = "-----BEGIN CERTIFICATE-----"
END_MARK = "-----END CERTIFICATE-----"


# ---------------------------------------------------------------- 工具函数
def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_admin():
    """以管理员身份重新运行本程序 (UAC)。"""
    params = " ".join('"%s"' % a for a in sys.argv)
    try:
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1
        )
    finally:
        sys.exit(0)


def fmt_time(ts):
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "?"


def inf_name_from_path(path):
    """从 ...\\<inf>.inf_amd64_xxx\\nvxdapix.dll 提取 inf 名。"""
    folder = os.path.basename(os.path.dirname(path))
    # 形如 nvgridsw.inf_amd64_847af0d59d1a7293
    m = re.match(r"^(.*?)(?:\.inf)?(?:_amd64.*)?$", folder)
    name = m.group(1) if m else folder
    return name


def _pnputil_oem_map():
    """解析 pnputil /enum-drivers, 返回 {发布名称oemXXX.inf: 原始名称xxx.inf}。

    输出为 GBK 中文, 结构固定 (每块 7 行, 空行分隔):
        发布名称:     oem247.inf
        原始名称:      nv_dispsi.inf
        ...
    """
    mapping = {}
    try:
        r = subprocess.run(["pnputil", "/enum-drivers"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        out = r.stdout or ""
    except Exception:
        return mapping
    cur_oem = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("发布名称"):
            # 发布名称:     oem247.inf
            val = line.split(":", 1)[1].strip() if ":" in line else ""
            cur_oem = val
        elif line.startswith("原始名称") and cur_oem:
            val = line.split(":", 1)[1].strip() if ":" in line else ""
            mapping[cur_oem] = val
            cur_oem = None
    return mapping


def get_active_nvidia_drivers():
    """查询当前实际加载的 NVIDIA 显卡驱动。

    返回 [(显卡名称, 原始inf名), ...]  (每块 NVIDIA 显卡一条, 多卡全查)
    通过 WMI Win32_PnPSignedDriver 拿 oemXXX.inf, 再用 pnputil 映射回原始 inf 名。
    """
    result = []
    ps = r"""
Get-CimInstance Win32_PnPSignedDriver | Where-Object { $_.Manufacturer -match "NVIDIA" -and ($_.DeviceName -match "GeForce|RTX|Quadro|Tesla|vGPU|GRID") } | ForEach-Object { Write-Output ($_.DeviceName + "|" + $_.INFName) }
"""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, encoding="gbk", errors="replace",
                           timeout=60)
        oem_lines = [l for l in (r.stdout or "").splitlines() if "|" in l]
    except Exception:
        return result

    if not oem_lines:
        return result

    oem_map = _pnputil_oem_map()
    for l in oem_lines:
        dev, oem = l.split("|", 1)
        dev, oem = dev.strip(), oem.strip()
        if not oem:
            continue
        orig_inf = oem_map.get(oem, "")
        result.append((dev, orig_inf))
    return result


def scan_nvxdapix():
    """返回 [(path, inf_name, ctime_str), ...]"""
    results = []
    if not os.path.isdir(DRV_ROOT):
        return results
    for root, _dirs, files in os.walk(DRV_ROOT):
        for fn in files:
            if fn.lower() == "nvxdapix.dll":
                full = os.path.join(root, fn)
                try:
                    ts = os.path.getctime(full)
                except OSError:
                    ts = 0
                results.append((full, inf_name_from_path(full), fmt_time(ts)))
    results.sort(key=lambda x: os.path.getctime(x[0]) if os.path.exists(x[0]) else 0)
    return results


def validate_cert_text(text):
    """校验证书文本。返回 (ok, error_msg, normalized_text)。"""
    if not text or not text.strip():
        return False, "证书内容为空。", ""
    if BEGIN_MARK not in text:
        return False, "缺少 %s 开头行。" % BEGIN_MARK, ""
    if END_MARK not in text:
        return False, "缺少 %s 结尾行。" % END_MARK, ""

    # 截取 BEGIN 与 END 之间的 base64 体
    try:
        body = text.split(BEGIN_MARK, 1)[1].split(END_MARK, 1)[0]
    except Exception:
        return False, "证书结构异常。", ""
    b64 = "".join(body.split())  # 去掉空白
    if not b64:
        return False, "证书 body 为空 (BEGIN/END 之间无内容)。", ""
    # 校验 base64 合法性
    try:
        base64.b64decode(b64, validate=True)
    except Exception as e:
        return False, "base64 解码失败: %s" % e, ""

    # 规范化为每行 64 字符的 PEM
    normalized = BEGIN_MARK + "\n"
    for i in range(0, len(b64), 64):
        normalized += b64[i:i + 64] + "\n"
    normalized += END_MARK + "\n"
    return True, "", normalized


# ---------------------------------------------------------------- 主界面
class PatcherApp:
    def __init__(self, root):
        self.root = root
        root.title("NVIDIA vGPU nvxdapix.dll 补丁工具")
        root.geometry("900x640")
        root.minsize(760, 540)

        self.dll_vars = {}   # path -> BooleanVar
        self.cert_text = tk.Text(root, height=8, wrap="none")
        self.ui_queue = queue.Queue()

        self._build_ui()

        if not is_admin():
            messagebox.showwarning(
                "需要管理员权限",
                "本工具需要覆盖系统驱动文件并重启服务, 请以管理员身份运行。\n\n"
                "是否立即以管理员身份重新启动?",
            )
            if messagebox.askyesno("确认", "以管理员身份重新启动?"):
                relaunch_admin()

        self._refresh_backup_list()
        self.refresh_dlls()
        self._poll_ui_queue()

    # ---------------- UI ----------------
    def _build_ui(self):
        pad = dict(padx=8, pady=4)

        # --- 顶部: DLL 列表 ---
        top = ttk.LabelFrame(self.root, text=" 1. 选择要打补丁的 nvxdapix.dll (可多选) ")
        top.pack(fill="both", expand=True, **pad)

        listframe = ttk.Frame(top)
        listframe.pack(fill="both", expand=True, padx=4, pady=2)

        self.listbox = tk.Listbox(listframe, selectmode="extended", activestyle="none")
        self.listbox.pack(side="left", fill="both", expand=True)

        sb = ttk.Scrollbar(listframe, orient="vertical", command=self.listbox.yview)
        sb.pack(side="right", fill="y")
        self.listbox.config(yscrollcommand=sb.set)
        self.listbox.bind("<ButtonRelease-1>", self._on_listbox_click)

        btnrow = ttk.Frame(top)
        btnrow.pack(fill="x", padx=4, pady=2)
        ttk.Button(btnrow, text="刷新扫描", command=self.refresh_dlls).pack(side="left")
        ttk.Button(btnrow, text="全选", command=lambda: self._set_all(True)).pack(side="left", padx=4)
        ttk.Button(btnrow, text="全不选", command=lambda: self._set_all(False)).pack(side="left")
        self.selected_lbl = ttk.Label(btnrow, text="已选: 0")
        self.selected_lbl.pack(side="right")

        # --- 中部: 证书 ---
        cert = ttk.LabelFrame(self.root, text=" 2. 根证书 (选择文件 或 直接粘贴内容) ")
        cert.pack(fill="x", **pad)

        crow = ttk.Frame(cert)
        crow.pack(fill="x", padx=4, pady=2)
        ttk.Button(crow, text="选择证书文件 (.pem)", command=self.choose_cert_file).pack(side="left")
        self.cert_path_lbl = ttk.Label(crow, text="未选择文件")
        self.cert_path_lbl.pack(side="left", padx=8)
        ttk.Button(crow, text="校验证书", command=self.on_validate).pack(side="right")
        self.cert_status = ttk.Label(crow, text="", foreground="gray")
        self.cert_status.pack(side="right", padx=8)

        self.cert_text.pack(fill="x", padx=4, pady=2)
        self.cert_text.insert("1.0", "")

        # --- 底部: 执行 + 日志 ---
        bot = ttk.LabelFrame(self.root, text=" 3. 执行 ")
        bot.pack(fill="both", expand=True, **pad)

        erow = ttk.Frame(bot)
        erow.pack(fill="x", padx=4, pady=2)
        self.run_btn = ttk.Button(erow, text="开始打补丁", command=self.run_patch, state="disabled")
        self.run_btn.pack(side="left")
        ttk.Button(erow, text="打开日志目录", command=self.open_log_dir).pack(side="left", padx=6)
        self.progress = ttk.Label(erow, text="就绪", foreground="green")
        self.progress.pack(side="right")

        # 备份恢复行
        brow = ttk.Frame(bot)
        brow.pack(fill="x", padx=4, pady=2)
        ttk.Label(brow, text="备份:").pack(side="left")
        self.backup_var = tk.StringVar()
        self.backup_combo = ttk.Combobox(brow, textvariable=self.backup_var,
                                         state="readonly", width=34)
        self.backup_combo.pack(side="left", padx=6)
        ttk.Button(brow, text="刷新备份", command=self._refresh_backup_list).pack(side="left")
        self.restore_btn = ttk.Button(brow, text="恢复选中备份", command=self.run_restore,
                                      state="disabled")
        self.restore_btn.pack(side="left", padx=6)
        self.backup_combo.bind("<<ComboboxSelected>>", self._on_backup_selected)

        logframe = ttk.Frame(bot)
        logframe.pack(fill="both", expand=True, padx=4, pady=2)
        self.log = tk.Text(logframe, height=10, wrap="word", state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        lsb = ttk.Scrollbar(logframe, orient="vertical", command=self.log.yview)
        lsb.pack(side="right", fill="y")
        self.log.config(yscrollcommand=lsb.set)

    # ---------------- DLL 列表 ----------------
    def refresh_dlls(self):
        self.log_msg("正在扫描 %s ..." % DRV_ROOT)
        self.root.update_idletasks()
        found = scan_nvxdapix()

        # 查询当前实际加载的 NVIDIA 显卡驱动 (多卡全查), 用于默认勾选
        self.log_msg("正在通过 WMI 查询当前显卡实际加载的驱动...")
        active = get_active_nvidia_drivers()
        active_infs = set()
        for dev, inf in active:
            if inf:
                n = inf[:-4] if inf.endswith(".inf") else inf
                active_infs.add(n.lower())
        if active:
            for dev, inf in active:
                self.log_msg("  检测到显卡: %s (驱动 inf: %s)" % (dev, inf or "未知"))
        else:
            self.log_msg("  未检测到已加载的 NVIDIA 显卡驱动, 不自动勾选。")

        self.listbox.delete(0, "end")
        self.dll_vars.clear()
        for path, inf, ts in found:
            label = "[%s.inf]  %s\n    %s" % (inf, ts, path)
            self.listbox.insert("end", label)
            # inf 名与当前加载驱动比对, 匹配则默认勾选
            matched = inf.lower() in active_infs
            self.dll_vars[path] = tk.BooleanVar(value=matched)
            if matched:
                self.log_msg("根据驱动 %s，目前显卡实际使用的驱动路径为 %s" % (
                    self._match_dev(inf, active), path))
        self.log_msg("扫描完成, 共找到 %d 个 nvxdapix.dll。" % len(found))
        if not found:
            messagebox.showwarning(
                "未找到",
                "未在 %s 找到 nvxdapix.dll。\n请确认已安装 NVIDIA vGPU (GRID) 驱动。" % DRV_ROOT,
            )
        self._update_selected_count()

    def _match_dev(self, inf, active):
        """返回与 inf 匹配的显卡名称 (用于日志)。"""
        for dev, ainf in active:
            n = (ainf or "")[:-4] if (ainf or "").endswith(".inf") else ainf
            if n.lower() == inf.lower():
                return dev
        return "NVIDIA"

    def _set_all(self, val):
        for v in self.dll_vars.values():
            v.set(val)
        self._update_selected_count()

    def _on_listbox_click(self, event):
        """点击 listbox 项目时切换对应 DLL 的勾选状态。"""
        if not (0 <= event.widget.nearest(event.y) < event.widget.size()):
            return
        idx = event.widget.nearest(event.y)
        sel = event.widget.get(idx)
        p = sel.split("\n    ", 1)[1] if "\n    " in sel else sel
        if p in self.dll_vars:
            self.dll_vars[p].set(not self.dll_vars[p].get())
            self._update_selected_count()

    def _update_selected_count(self):
        n = sum(1 for v in self.dll_vars.values() if v.get())
        self.selected_lbl.config(text="已选: %d" % n)
        self.run_btn.config(state="normal" if n > 0 else "disabled")

    def selected_paths(self):
        # 列表顺序与 dll_vars 一致 (插入顺序)
        order = []
        for i in range(self.listbox.size()):
            sel = self.listbox.get(i)
            path = sel.split("\n    ", 1)[1] if "\n    " in sel else sel
            if path in self.dll_vars and self.dll_vars[path].get():
                order.append(path)
        return order

    # ---------------- 证书 ----------------
    def choose_cert_file(self):
        p = filedialog.askopenfilename(
            title="选择根证书文件",
            filetypes=[("PEM 证书", "*.pem *.crt *.cer"), ("所有文件", "*.*")],
        )
        if not p:
            return
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            messagebox.showerror("读取失败", "无法读取文件:\n%s" % e)
            return
        self.cert_text.delete("1.0", "end")
        self.cert_text.insert("1.0", content)
        self.cert_path_lbl.config(text=os.path.basename(p))
        self.on_validate()

    def on_validate(self):
        text = self.cert_text.get("1.0", "end")
        ok, err, _ = validate_cert_text(text)
        if ok:
            self.cert_status.config(text="证书校验通过", foreground="green")
            self._last_norm = _
        else:
            self.cert_status.config(text="校验失败: " + err, foreground="red")
            self._last_norm = None
        return ok

    # ---------------- 执行 ----------------
    def run_patch(self):
        # 1) 校验证书
        text = self.cert_text.get("1.0", "end")
        ok, err, normalized = validate_cert_text(text)
        if not ok:
            self.cert_status.config(text="校验失败: " + err, foreground="red")
            messagebox.showerror("证书无效", "打补丁前必须先通过证书校验。\n\n" + err)
            return

        paths = self.selected_paths()
        if not paths:
            return

        # 2) 写临时证书到程序同路径的 temp 文件夹
        tdir = temp_dir()
        cert_tmp = os.path.join(tdir, "gridd_rootcert_%d.pem" % int(datetime.now().timestamp() % 10**8))
        try:
            with open(cert_tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(normalized)
        except Exception as e:
            messagebox.showerror("写入失败", "无法写入临时证书:\n%s" % e)
            return

        if not os.path.exists(PATCHER_EXE):
            messagebox.showerror(
                "缺少工具",
                "未找到 gridd-unlock-patcher.exe。\n请将其放到主程序同目录:\n%s" % app_dir(),
            )
            return

        # 创建本次备份会话目录 (unix 时间戳)
        session_dir, ts = backup_session_dir()
        self.log_msg("=" * 60)
        self.log_msg("备份会话目录: %s" % session_dir)
        self.log_msg("开始处理 %d 个 DLL, 证书: %s" % (len(paths), cert_tmp))
        # 立即禁用按钮 + 提示处理中 (主线程)
        self.run_btn.config(state="disabled")
        self.restore_btn.config(state="disabled")
        self.progress.config(text="处理中...", foreground="orange")
        self.root.update()

        # 耗时操作放到子线程, 避免冻结 UI
        t = threading.Thread(
            target=self._patch_worker,
            args=(paths, cert_tmp, session_dir, tdir),
            daemon=True,
        )
        t.start()

    def _patch_worker(self, paths, cert_tmp, session_dir, tdir):
        """子线程: 执行所有耗时操作 (备份/打补丁/停服务/覆盖)。"""
        manifest_entries = []
        total = len(paths)
        # 将选中的 DLL 复制到 temp 文件夹, 用相对路径执行 patcher
        tmp_paths = []
        for i, path in enumerate(paths, 1):
            tmp_name = "nvxdapix_%d.dll" % i
            tmp_dll = os.path.join(tdir, tmp_name)
            self.log_msg("复制 [%d/%d] %s -> %s" % (i, total, os.path.basename(path), tmp_dll))
            r = self._run_cmd(["cmd", "/c", "copy", "/y", path, tmp_dll], "复制待打补丁 DLL 到 temp")
            if r != 0 or not os.path.exists(tmp_dll):
                self.log_msg("[错误] 复制失败 (返回码 %s), 跳过该 DLL。" % r)
                continue
            tmp_paths.append((path, tmp_dll))
        if not tmp_paths:
            self.log_msg("[错误] 没有可处理的 DLL, 终止。")
        for idx, (path, tmp_dll) in enumerate(tmp_paths, 1):
            self._ui(lambda i=idx: self.progress.config(
                text="处理 %d/%d ..." % (i, len(tmp_paths)), foreground="orange"))
            self._patch_one(path, tmp_dll, cert_tmp, session_dir=session_dir,
                            manifest_entries=manifest_entries)
        # 写 manifest
        if manifest_entries:
            write_manifest(session_dir, manifest_entries)
            self.log_msg("[备份] 本次共备份 %d 个 DLL, 清单已写入 %s" % (
                len(manifest_entries), os.path.join(session_dir, "manifest.json")))
        # 回到主线程更新 UI 状态
        def _done():
            self.progress.config(text="全部完成", foreground="green")
            self.run_btn.config(state="normal")
            self.restore_btn.config(state="normal")
            self._refresh_backup_list()
            # 完成提示只写日志 + 进度标签, 不用模态弹窗 (避免阻塞 UI)
            self.log_msg("=" * 60)
            self.log_msg("[完成] 所有操作结束。请重启虚拟机使补丁生效。")
            self.log_msg("       若服务异常, 可在上方【备份】列表选中会话点【恢复选中备份】回滚。")
            self.log_msg("=" * 60)
        self._ui(_done)

    def _patch_one(self, dll_path, tmp_dll, cert_tmp, session_dir=None, manifest_entries=None):
        self.log_msg("-" * 60)
        self.log_msg("处理: %s" % dll_path)
        self.log_msg("temp 副本: %s" % tmp_dll)

        # 备份原始 dll 到会话目录 (保留 DriverStore 相对路径结构)
        if session_dir and manifest_entries is not None:
            rel = backup_one_dll(dll_path, session_dir, log=self.log_msg)
            if rel is None:
                self.log_msg("[错误] 备份失败, 跳过该 DLL。")
                return
            manifest_entries.append((dll_path, rel))
        else:
            # 兜底: 无会话目录时仍做 .bak
            bak = dll_path + ".bak"
            self._run_cmd(["cmd", "/c", "copy", "/y", dll_path, bak], "备份原 DLL")
            if not os.path.exists(bak):
                self.log_msg("[错误] 备份失败, 跳过该 DLL。")
                return

        # 对 temp 副本打补丁: 切换工作目录到程序同路径, 用相对路径执行
        # (规避绝对路径含中文/空格导致的兼容性问题, 同时便于排查)
        tdir = os.path.dirname(tmp_dll)
        tmp_rel = os.path.basename(tmp_dll)
        cert_rel = os.path.basename(cert_tmp)
        cmd = [os.path.basename(PATCHER_EXE), "-g", tmp_rel, "-c", cert_rel]
        self.log_msg("调用 (工作目录 %s): %s" % (tdir, " ".join(cmd)))
        r = self._run_cmd(cmd, "执行 gridd-unlock-patcher", cwd=tdir)
        if r != 0 or not os.path.exists(tmp_dll):
            if r == 0xC0000005:
                self.log_msg("[错误] 补丁程序崩溃 (0xC0000005 内存访问违规)。")
                self.log_msg("       该 nvxdapix.dll 可能不是 GRID/vGPU 驱动版本, 补丁工具不支持此类型。")
                self.log_msg("       请确认目标显卡为 vGPU 环境 (非桌面 GeForce/Tesla 驱动)。")
            else:
                self.log_msg("[错误] 补丁程序执行失败 (返回码 %s), 跳过覆盖。" % r)
            self._cleanup(tmp_dll)
            return

        # 停服务
        self._run_cmd(["cmd", "/c", "net", "stop", SVC_NAME], "停止服务 " + SVC_NAME,
                      allow_fail=True)

        # 兜底: 停服务后仍可能有其他进程占用该 DLL, 用 psutil 强杀 (对齐 ps1 逻辑)
        self.log_msg("  检查是否有其他进程仍占用该 DLL ...")
        n = kill_processes_using_dll(dll_path, log=self.log_msg)
        if n:
            self.log_msg("  已强杀 %d 个仍占用 DLL 的进程。" % n)
        else:
            self.log_msg("  无其他进程占用, 继续。")

        # takeown + icacls
        self._run_cmd(["cmd", "/c", "takeown", "/f", dll_path], "获取所有权", allow_fail=True)
        self._run_cmd(["cmd", "/c", "icacls", dll_path, "/grant", "Administrators:F"],
                      "授予管理员完全控制", allow_fail=True)

        # 覆盖
        rc = self._run_cmd(["cmd", "/c", "copy", "/y", tmp_dll, dll_path], "覆盖目标 DLL")
        if rc != 0:
            self.log_msg("[错误] 覆盖失败 (返回码 %s)。" % rc)
        else:
            self.log_msg("[成功] 已覆盖 %s" % dll_path)

        # 起服务
        self._run_cmd(["cmd", "/c", "net", "start", SVC_NAME], "重启服务 " + SVC_NAME,
                      allow_fail=True)

        self._cleanup(tmp_dll)

    def _cleanup(self, f):
        try:
            if f and os.path.exists(f):
                os.remove(f)
        except Exception:
            pass

    # ---------------- 命令执行 ----------------
    def _run_cmd(self, cmd, desc, allow_fail=False, cwd=None):
        self.log_msg("  >> %s" % desc)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="gbk", errors="replace", cwd=cwd)
            out = (p.stdout or "").strip()
            err = (p.stderr or "").strip()
            if out:
                for ln in out.splitlines():
                    self.log_msg("     | " + ln)
            if err:
                for ln in err.splitlines():
                    self.log_msg("     ! " + ln)
            if p.returncode != 0 and not allow_fail:
                self.log_msg("     [失败] 返回码 %d" % p.returncode)
            return p.returncode
        except Exception as e:
            self.log_msg("     [异常] %s" % e)
            return -1

    # ---------------- 日志 ----------------
    def _poll_ui_queue(self):
        """主线程轮询 UI 队列, 执行子线程提交的 UI 操作。"""
        try:
            while True:
                fn = self.ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.root.after(50, self._poll_ui_queue)

    def _ui(self, fn):
        """线程安全的 UI 调度: 子线程入队, 主线程轮询执行。"""
        if threading.current_thread() is threading.main_thread():
            fn()
        else:
            self.ui_queue.put(fn)

    def log_msg(self, msg):
        """线程安全日志: 子线程入队, 主线程执行。"""
        def _do():
            self.log.config(state="normal")
            self.log.insert("end", msg + "\n")
            self.log.see("end")
            self.log.config(state="disabled")
        self._ui(_do)

    def open_log_dir(self):
        os.startfile(tempfile.gettempdir())

    # ---------------- 备份 / 恢复 ----------------
    def _refresh_backup_list(self, select_first=True):
        sessions = list_backup_sessions()
        self._sessions = {}
        labels = []
        for d, ts, cstr in sessions:
            label = "%s  (ts=%d)" % (cstr, ts)
            labels.append(label)
            self._sessions[label] = d
        self.backup_combo["values"] = tuple(labels)
        if labels:
            if select_first:
                self.backup_combo.current(0)
            self.restore_btn.config(state="normal")
        else:
            self.backup_var.set("")
            self.restore_btn.config(state="disabled")
        return sessions

    def _on_backup_selected(self, _evt=None):
        self.restore_btn.config(state="normal" if self.backup_var.get() else "disabled")

    def _selected_session_dir(self):
        label = self.backup_var.get()
        return self._sessions.get(label)

    def run_restore(self):
        session_dir = self._selected_session_dir()
        if not session_dir:
            messagebox.showwarning("提示", "请先在备份列表选择一个要恢复的备份。")
            return
        entries = read_manifest(session_dir)
        if not entries:
            messagebox.showerror("错误", "该备份目录无有效清单 (manifest.json)。")
            return
        # 检查备份文件是否都在
        missing = [orig for orig, rel in entries
                   if not os.path.exists(os.path.join(session_dir, rel))]
        if missing:
            messagebox.showerror(
                "备份不完整",
                "以下原始 DLL 的备份文件缺失, 无法完整恢复:\n\n"
                + "\n".join(missing),
            )
            return

        if not messagebox.askyesno(
                "确认恢复",
                "即将从备份恢复到以下 %d 个原始位置 (会覆盖当前文件):\n\n%s\n\n"
                "恢复会停止显示服务并重启, 确认继续?" % (
                    len(entries), "\n".join(o for o, _ in entries)),
        ):
            return

        self.log_msg("=" * 60)
        self.log_msg("[恢复] 从会话 %s 恢复 %d 个 DLL" % (session_dir, len(entries)))
        # 立即禁用 + 提示 (主线程)
        self.run_btn.config(state="disabled")
        self.restore_btn.config(state="disabled")
        self.progress.config(text="恢复中...", foreground="orange")
        self.root.update()

        # 耗时操作放子线程
        threading.Thread(target=self._restore_worker, args=(session_dir, entries),
                         daemon=True).start()

    def _restore_worker(self, session_dir, entries):
        """子线程: 执行恢复的所有耗时操作。"""
        # 停服务 (一次即可, 恢复完统一重启)
        self._run_cmd(["cmd", "/c", "net", "stop", SVC_NAME], "停止服务 " + SVC_NAME,
                      allow_fail=True)
        for orig, rel in entries:
            self._restore_one(orig, os.path.join(session_dir, rel))
        self._run_cmd(["cmd", "/c", "net", "start", SVC_NAME], "重启服务 " + SVC_NAME,
                      allow_fail=True)
        def _done():
            self.progress.config(text="恢复完成", foreground="green")
            self.run_btn.config(state="normal")
            self.restore_btn.config(state="normal")
            self.log_msg("=" * 60)
            self.log_msg("[恢复完成] 已从备份恢复 %d 个 DLL。请重启虚拟机使恢复生效。" % len(entries))
            self.log_msg("=" * 60)
        self._ui(_done)

    def _restore_one(self, original_path, backup_file):
        self.log_msg("-" * 60)
        self.log_msg("恢复: %s" % original_path)
        if not os.path.exists(backup_file):
            self.log_msg("[错误] 备份文件不存在: %s, 跳过。" % backup_file)
            return
        # 兜底: 恢复前再强杀占用进程
        self.log_msg("  检查是否有进程仍占用目标 DLL ...")
        kill_processes_using_dll(original_path, log=self.log_msg)
        # takeown + icacls
        self._run_cmd(["cmd", "/c", "takeown", "/f", original_path], "获取所有权", allow_fail=True)
        self._run_cmd(["cmd", "/c", "icacls", original_path, "/grant", "Administrators:F"],
                      "授予管理员完全控制", allow_fail=True)
        # 从备份覆盖回原路径
        rc = self._run_cmd(["cmd", "/c", "copy", "/y", backup_file, original_path],
                           "用备份覆盖回原路径")
        if rc != 0:
            self.log_msg("[错误] 恢复失败 (返回码 %s): %s" % (rc, original_path))
        else:
            self.log_msg("[成功] 已恢复 %s" % original_path)



def _preempt_meipass_cleanup():
    """在 PyInstaller 的 _MEIPASS 清理逻辑之前, 先移走临时目录。

    PyInstaller onefile 模式退出时会尝试删除 _MEIPASS 目录, 若失败则弹 Warning 窗口。
    此钩子先于其执行 (atexit 后进先出), 把 _MEIPASS 移到不受保护的临时位置, 使 PyInstaller
    后续删除时因目录已不存在而静默跳过, 不再弹窗。残留目录由 Windows 重启后自动清理。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass or not os.path.isdir(meipass):
        return
    try:
# 直接删除而非移动: 移动后 PyInstaller 清理逻辑仍会尝试删原始路径,
# 若文件句柄未释放则弹窗。rmtree 删除后原始路径不存在, PyInstaller 静默跳过。
        for retry in range(3):
            try:
                shutil.rmtree(meipass, ignore_errors=False)
                break
            except Exception:
                if retry < 2:
                    import time
                    time.sleep(0.5)
                else:
                    shutil.rmtree(meipass, ignore_errors=True)
    except Exception:
        pass


def main():
    # 注册 _MEIPASS 清理前置钩子 (打包 onefile 模式时防止退出弹窗)
    atexit.register(_preempt_meipass_cleanup)

    # 启动时优先检查同文件夹下的 gridd-unlock-patcher.exe
    _same_dir_patcher = os.path.join(app_dir(), "gridd-unlock-patcher.exe")
    if not os.path.exists(_same_dir_patcher):
        # 同目录没有; 若打包内置的在, 也允许(提示但继续)
        _embedded = getattr(sys, "_MEIPASS", None)
        _embedded_ok = bool(_embedded) and os.path.exists(
            os.path.join(_embedded, "gridd-unlock-patcher.exe"))
        if not _embedded_ok:
            root0 = tk.Tk()
            root0.withdraw()
            messagebox.showerror(
                "缺少补丁工具",
                "未找到 gridd-unlock-patcher.exe。\n\n"
                "该文件是补丁核心, 必须与主程序放在同一目录:\n"
                "    %s\n\n"
                "请把 gridd-unlock-patcher.exe 放到该目录后重新运行。"
                % app_dir(),
            )
            root0.destroy()
            sys.exit(1)
        # 仅内置存在: 提示后继续
        root0 = tk.Tk()
        root0.withdraw()
        messagebox.showwarning(
            "提示",
            "主程序同目录下未找到 gridd-unlock-patcher.exe,\n"
            "将使用内置副本。建议将其放到主程序同目录:\n"
            "    %s" % app_dir(),
        )
        root0.destroy()

    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
    except Exception:
        pass
    PatcherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
