import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import json
import os
import sys
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
        self.geometry("900"x"650")
        self.minsize(800, 550)

        self.scan_root = tk.StringVar()
        self.report_output = tk.StringVar(value=str(Path.cwd() / "清理报告"))
        self.current_plan = None

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

        self.btn_preview = ttk.Button(btn_frame, text="🔍 开始预览扫描 (仅分析)", command=self._run_preview)
        self.btn_preview.pack(side="left", padx=5)

        self.btn_apply = ttk.Button(btn_frame, text="📦 执行安全隔离 (移动到隔离区)", command=self._run_apply, state="disabled")
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

    def _browse_root(self):
        path = filedialog.askdirectory(title="选择待扫描根目录")
        if path:
            self.scan_root.set(path)

    def _browse_output(self):
        path = filedialog.askdirectory(title="选择报告保存目录")
        if path:
            self.report_output.set(path)

    def _run_preview(self):
        root_path = self.scan_root.get().strip()
        out_path = self.report_output.get().strip()

        if not root_path or not os.path.isdir(root_path):
            messagebox.showerror("错误", "请指定有效的待扫描根目录！")
            return

        if not out_path:
            messagebox.showerror("错误", "请指定报告保存目录！")
            return

        try:
            self.status_var.set("正在扫描与生成对比分析中，请稍候...")
            self.update_idletasks()

            self.current_plan = cleaner.plan(root_path)
            report_file = cleaner.execute(self.current_plan, out_path, apply=False)

            # 更新表格
            for item in self.tree.get_children():
                self.tree.delete(item)

            jobs = self.current_plan.get("jobs", [])
            for job in jobs:
                kind_text = "📁 文件夹" if job["kind"] == "folder" else "📄 文件"
                self.tree.insert("", "end", values=(kind_text, job["keeper"], job["source"]))

            self.status_var.set(f"扫描完成！找到 {len(jobs)} 项待隔离项目。已生成报告：{report_file.name}")
            
            if jobs:
                self.btn_apply.config(state="normal")
            else:
                self.btn_apply.config(state="disabled")
                messagebox.showinfo("提示", "未发现可清理的重复项目！")

        except Exception as e:
            self.status_var.set("扫描失败。")
            messagebox.showerror("错误", f"扫描过程中出错：\n{e}")

    def _run_apply(self):
        if not self.current_plan or not self.current_plan.get("jobs"):
            return

        count = len(self.current_plan["jobs"])
        confirm = messagebox.askyesno("安全隔离确认", f"确定要将这 {count} 项重复内容移动到隔离区吗？\n\n注意：操作仅进行安全隔离，不会永久删除任何数据。")
        if not confirm:
            return

        root_path = self.scan_root.get().strip()
        out_path = self.report_output.get().strip()

        try:
            self.status_var.set("正在安全隔离中...")
            self.update_idletasks()

            report_file = cleaner.execute(self.current_plan, out_path, apply=True)
            self.status_var.set(f"隔离完成！隔离文件位于根目录的 `_重复文件隔离区`，详细日志请见：{report_file.name}")
            messagebox.showinfo("成功", "重复项目已成功隔离！数据 100% 保留，未执行任何删除。")
            self.btn_apply.config(state="disabled")

        except Exception as e:
            self.status_var.set("隔离操作已中断。")
            messagebox.showerror("错误", f"执行隔离时出错：\n{e}")

if __name__ == "__main__":
    app = SafeCleanerGUI()
    app.mainloop()
