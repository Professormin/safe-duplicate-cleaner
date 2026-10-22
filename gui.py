import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import json
import os
import sys
import threading
from pathlib import Path

# 导入核心清理逻辑
import importlib.util
spec = importlib.util.spec_from_file_location("cleaner", Path(__file__).parent / "两级重复文件安全清理.py")
cleaner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleaner)

class SafeCleanerGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Safe Duplicate Cleaner · 两级重复文件安全清理工具")
        self.geometry("900x650")
        self.minsize(800, 550)

        self.scan_root = tk.StringVar()
        self.scan_root.trace_add("write", self._on_path_change)
        self.report_output = tk.StringVar(value=str(Path.cwd() / "清理报告"))
        
        self.current_plan = None
        self.plan_root = None  # 记录计划对应的根路径
        self.is_running = False

        self._build_ui()

    def _build_ui(self):
        # 顶部配置区域
        config_frame = ttk.LabelFrame(self, text=" 路径设置 ", padding=10)
        config_frame.pack(fill="x", padx=15, pady=10)

        # 扫描路径
        ttk.Label(config_frame, text="待扫描根目录:").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(config_frame, textvariable=self.scan_root, width=60).grid(row=0, column=1, padx=5, sticky="ew")
        ttk.Button(config_frame, text="浏览...", command=self._browse_root).grid(row=0, column=2, padx=5)

        # 报告路径
        ttk.Label(config_frame, text="报告保存目录:").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(config_frame, textvariable=self.report_output, width=60).grid(row=1, column=1, padx=5, sticky="ew")
        ttk.Button(config_frame, text="浏览...", command=self._browse_output).grid(row=1, column=2, padx=5)

        config_frame.columnconfigure(1, weight=1)

        # 操作按钮区
        btn_frame = ttk.Frame(self, padding=5)
        btn_frame.pack(fill="x", padx=15)

        self.btn_preview = ttk.Button(btn_frame, text="🔍 开始预览扫描 (仅分析)", command=self._start_preview_thread)
        self.btn_preview.pack(side="left", padx=5)

        self.btn_apply = ttk.Button(btn_frame, text="📦 执行安全隔离 (移动到隔离区)", command=self._start_apply_thread, state="disabled")
        self.btn_apply.pack(side="left", padx=5)

        # 结果与选项卡
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=15, pady=10)

        # 预览结果表格
        tab_tree = ttk.Frame(notebook)
        notebook.add(tab_tree, text=" 重复列表预览 ")

        columns = ("kind", "keeper", "source")
        self.tree = ttk.Treeview(tab_tree, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("kind", text="类型")
        self.tree.heading("keeper", text="保留副本 (Keeper)")
        self.tree.heading("source", text="待隔离副本 (Source)")

        self.tree.column("kind", width=100, anchor="center")
        self.tree.column("keeper", width=350)
        self.tree.column("source", width=350)

        scrollbar = ttk.Scrollbar(tab_tree, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=scrollbar.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # 状态栏
        self.status_var = tk.StringVar(value="就绪。请选择待扫描根目录。")
        status_bar = ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w", padding=5)
        status_bar.pack(fill="x", side="bottom")

    def _on_path_change(self, *args):
        """当用户修改路径输入框时，清空旧计划并重置按钮状态，避免路径与计划不一致。"""
        if self.current_plan:
            self.current_plan = None
            self.plan_root = None
            self.btn_apply.config(state="disabled")
            for item in self.tree.get_children():
                self.tree.delete(item)
            self.status_var.set("扫描路径已更改，请重新点击“开始预览扫描”。")

    def _browse_root(self):
        path = filedialog.askdirectory(title="选择待扫描根目录")
        if path:
            self.scan_root.set(path)

    def _browse_output(self):
        path = filedialog.askdirectory(title="选择报告保存目录")
        if path:
            self.report_output.set(path)

    def _start_preview_thread(self):
        if self.is_running:
            return
        root_path = self.scan_root.get().strip()
        out_path = self.report_output.get().strip()

        if not root_path or not os.path.isdir(root_path):
            messagebox.showerror("错误", "请指定有效的待扫描根目录！")
            return

        if not out_path:
            messagebox.showerror("错误", "请指定报告保存目录！")
            return

        self.is_running = True
        self.btn_preview.config(state="disabled")
        self.btn_apply.config(state="disabled")
        self.status_var.set("正在后台进行安全扫描与对比计算，请稍候...")

        threading.Thread(target=self._worker_preview, args=(root_path, out_path), daemon=True).start()

    def _worker_preview(self, root_path, out_path):
        try:
            plan = cleaner.plan(root_path)
            report_file = cleaner.execute(plan, out_path, apply=False)
            self.after(0, self._on_preview_success, plan, root_path, report_file)
        except Exception as e:
            self.after(0, self._on_preview_error, str(e))

    def _on_preview_success(self, plan, root_path, report_file):
        self.is_running = False
        self.btn_preview.config(state="normal")
        self.current_plan = plan
        self.plan_root = root_path

        for item in self.tree.get_children():
            self.tree.delete(item)

        jobs = plan.get("jobs", [])
        for job in jobs:
            kind_text = "📁 文件夹" if job["kind"] == "folder" else "📄 文件"
            self.tree.insert("", "end", values=(kind_text, job["keeper"], job["source"]))

        self.status_var.set(f"扫描完成！找到 {len(jobs)} 项待隔离项目。已生成报告：{report_file.name}")
        
        if jobs:
            self.btn_apply.config(state="normal")
        else:
            self.btn_apply.config(state="disabled")
            messagebox.showinfo("提示", "未发现可清理的重复项目！")

    def _on_preview_error(self, err_msg):
        self.is_running = False
        self.btn_preview.config(state="normal")
        self.status_var.set("扫描失败。")
        messagebox.showerror("错误", f"扫描过程中出错：\n{err_msg}")

    def _start_apply_thread(self):
        if self.is_running or not self.current_plan or not self.current_plan.get("jobs"):
            return

        current_root = self.scan_root.get().strip()
        if current_root != self.plan_root:
            messagebox.showerror("错误", "路径已变更，请重新执行“预览扫描”后再试！")
            return

        count = len(self.current_plan["jobs"])
        confirm = messagebox.askyesno("安全隔离确认", f"确定要将这 {count} 项重复内容移动到隔离区吗？\n\n注意：操作仅进行安全隔离，不会永久删除任何数据。")
        if not confirm:
            return

        out_path = self.report_output.get().strip()
        self.is_running = True
        self.btn_preview.config(state="disabled")
        self.btn_apply.config(state="disabled")
        self.status_var.set("正在安全隔离中，请稍候...")

        threading.Thread(target=self._worker_apply, args=(out_path,), daemon=True).start()

    def _worker_apply(self, out_path):
        try:
            report_file = cleaner.execute(self.current_plan, out_path, apply=True)
            report_data = json.loads(Path(report_file).read_text(encoding="utf-8"))
            self.after(0, self._on_apply_success, report_file, report_data)
        except Exception as e:
            self.after(0, self._on_apply_error, str(e))

    def _on_apply_success(self, report_file, report_data):
        self.is_running = False
        self.btn_preview.config(state="normal")
        
        stats = report_data.get("statistics", {})
        moved = stats.get("moved_items", 0)
        skipped = stats.get("skipped_items", 0)

        if moved > 0:
            msg = f"已成功隔离 {moved} 项内容（跳过 {skipped} 项未变动/受变动项）。\n\n所有隔离内容均安全保存在 root/_重复文件隔离区 中，未进行任何删除。"
            messagebox.showinfo("隔离完成", msg)
            self.status_var.set(f"隔离完成：成功隔离 {moved} 项，跳过 {skipped} 项。日志：{report_file.name}")
        else:
            msg = f"未隔离任何项目（共跳过 {skipped} 项，可能因为源或保留副本在扫描后发生了变化或不存在）。"
            messagebox.showwarning("跳过提示", msg)
            self.status_var.set(f"隔离提示：未移动任何项目，跳过 {skipped} 项。日志：{report_file.name}")

        self.current_plan = None
        self.plan_root = None

    def _on_apply_error(self, err_msg):
        self.is_running = False
        self.btn_preview.config(state="normal")
        self.status_var.set("隔离操作已中断。")
        messagebox.showerror("错误", f"执行隔离时出错：\n{err_msg}")

if __name__ == "__main__":
    app = SafeCleanerGUI()
    app.mainloop()
