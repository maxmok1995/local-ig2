import os
import json
import re
import time
import threading
import queue
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from deep_translator import GoogleTranslator

# ── 全局状态（线程安全） ──────────────────────────────────────────
translator = None
translation_cache = {}
cache_lock = threading.Lock()
counter_lock = threading.Lock()
log_queue = queue.Queue()
ui_update_queue = queue.Queue()

total_tasks = 0
done_tasks = 0

MAX_WORKERS = 5
MAX_RETRIES = 3

# ── Emoji 处理 ────────────────────────────────────────────────────
emoji_pattern = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "]+", flags=re.UNICODE)


def extract_emoji(text):
    emojis = emoji_pattern.findall(text)
    clean_text = emoji_pattern.sub('', text).strip()
    return clean_text, ''.join(emojis)


def is_already_target_lang(text, target_lang):
    """简单判断是否已经是目标语言（避免重复翻译）"""
    if target_lang in ("en", "english"):
        return bool(re.match(r'^[\x00-\x7F\s\W]*$', text))
    return False


def translate_text(text):
    """翻译单条文本，支持任意语言源，含缓存 + 限重试次数"""
    if not text or not text.strip():
        return text

    clean_text, emojis = extract_emoji(text)

    if not clean_text:
        return text  # 只有 emoji，不翻译

    # ★ 核心修复：去掉原来"只翻译中文"的限制
    target_lang = translator._target if translator else "en"
    if is_already_target_lang(clean_text, target_lang):
        return text  # 已经是目标语言，跳过

    with cache_lock:
        if clean_text in translation_cache:
            return translation_cache[clean_text] + emojis

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = translator.translate(clean_text)
            if result and result.strip():
                with cache_lock:
                    translation_cache[clean_text] = result
                time.sleep(0.3)
                return result + emojis
            else:
                log_queue.put(f"[警告] 翻译返回空，尝试 {attempt}/{MAX_RETRIES}：{clean_text[:40]}")
        except Exception as e:
            log_queue.put(f"[错误] 翻译异常（{attempt}/{MAX_RETRIES}）：{e}")
        time.sleep(2)

    log_queue.put(f"[失败] 保留原文：{clean_text[:40]}")
    return None


def process_meta(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 诊断：打印第一个文件的内容
        if getattr(process_meta, '_first', True):
            process_meta._first = False
            log_queue.put(f"[诊断] 第一个 meta.json 内容：{str(data)[:300]}")

        changed = False
        if "caption" in data and data["caption"]:
            original = data["caption"]
            result = translate_text(original)
            if result is not None and result != original:
                data["caption"] = result
                changed = True
                log_queue.put(f"[✓] {os.path.basename(os.path.dirname(path))} caption 已翻译")
            elif result == original:
                log_queue.put(f"[=] 已是目标语言跳过：{original[:40]}")

        if changed:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    except Exception as e:
        log_queue.put(f"[错误] meta.json 处理失败 {path}：{e}")


def process_notes(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        changed = False
        folder_name = os.path.basename(os.path.dirname(path))

        for field in ("title", "desc"):
            if field in data and data[field]:
                original = data[field]
                result = translate_text(original)
                if result is not None and result != original:
                    data[field] = result
                    changed = True
                    log_queue.put(f"[✓] {folder_name} {field} 已翻译")

        if changed:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    except Exception as e:
        log_queue.put(f"[错误] notes.json 处理失败 {path}：{e}")


def worker(task):
    t, path = task
    try:
        if t == "meta":
            process_meta(path)
        elif t == "notes":
            process_notes(path)
    except Exception as e:
        log_queue.put(f"[worker 异常] {path}：{e}")
    finally:
        with counter_lock:
            global done_tasks
            done_tasks += 1
            current = done_tasks
        ui_update_queue.put(current)


def scan_folder(folder):
    tasks = []
    for dirpath, dirnames, files in os.walk(folder):
        if "meta.json" in files:
            tasks.append(("meta", os.path.join(dirpath, "meta.json")))
        if "notes.json" in files:
            tasks.append(("notes", os.path.join(dirpath, "notes.json")))
    return tasks


def run_with_pool(tasks):
    # ★ 先做连接测试，立刻暴露网络问题
    log_queue.put("[测试] 正在测试翻译服务连接...")
    try:
        test_result = translator.translate("สวัสดี")  # 泰语"你好"
        log_queue.put(f"[测试] ✅ 连接正常，测试翻译结果：{test_result}")
    except Exception as e:
        log_queue.put(f"[测试] ❌ 连接失败：{e}")
        log_queue.put("[测试] 请检查是否能访问 Google，如在大陆/老挝需要开启代理")
        ui_update_queue.put("done")
        return

    # 重置诊断标志
    process_meta._first = True

    task_queue = queue.Queue()
    for task in tasks:
        task_queue.put(task)

    def pool_worker():
        while True:
            try:
                task = task_queue.get_nowait()
            except queue.Empty:
                break
            worker(task)

    threads = [threading.Thread(target=pool_worker, daemon=True) for _ in range(MAX_WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log_queue.put("✅ 全部处理完成！")
    ui_update_queue.put("done")


def start_translate():
    folder = folder_var.get()
    lang = lang_var.get()

    if not folder:
        messagebox.showerror("错误", "请选择文件夹")
        return

    global translator, total_tasks, done_tasks
    translator = GoogleTranslator(source="auto", target=lang)

    tasks = scan_folder(folder)
    if not tasks:
        messagebox.showinfo("提示", "没有找到 meta.json / notes.json 文件")
        return

    total_tasks = len(tasks)
    done_tasks = 0
    progress["maximum"] = total_tasks
    progress["value"] = 0
    status_var.set(f"0/{total_tasks} (0%)")
    log(f"发现 {total_tasks} 个文件，并发线程数：{MAX_WORKERS}")

    start_btn.config(state=tk.DISABLED)
    threading.Thread(target=run_with_pool, args=(tasks,), daemon=True).start()


def select_folder():
    folder = filedialog.askdirectory()
    if folder:
        folder_var.set(folder)


def log(text):
    log_box.insert(tk.END, text + "\n")
    log_box.see(tk.END)


def poll_queues():
    while not log_queue.empty():
        try:
            msg = log_queue.get_nowait()
            log(msg)
        except queue.Empty:
            break

    while not ui_update_queue.empty():
        try:
            val = ui_update_queue.get_nowait()
            if val == "done":
                start_btn.config(state=tk.NORMAL)
                messagebox.showinfo("完成", "全部处理完成！")
            else:
                progress["value"] = val
                percent = int((val / total_tasks) * 100) if total_tasks else 0
                status_var.set(f"{val}/{total_tasks} ({percent}%)")
        except queue.Empty:
            break

    root.after(100, poll_queues)


# ── GUI ──────────────────────────────────────────────────────────
root = tk.Tk()
root.title("JSON 批量翻译工具 v3")
root.geometry("660x580")

folder_var = tk.StringVar()
lang_var = tk.StringVar(value="en")
status_var = tk.StringVar(value="就绪")

tk.Label(root, text="选择数据集文件夹").pack(pady=5)

frame = tk.Frame(root)
frame.pack()
tk.Entry(frame, textvariable=folder_var, width=52).pack(side=tk.LEFT)
tk.Button(frame, text="浏览", command=select_folder).pack(side=tk.LEFT, padx=4)

tk.Label(root, text="目标语言").pack(pady=5)
tk.OptionMenu(root, lang_var, "en", "zh-CN", "zh-TW", "ja", "ko", "fr", "de", "th").pack()

start_btn = tk.Button(root, text="开始翻译", command=start_translate,
                      bg="#4CAF50", fg="white", font=("Arial", 11, "bold"))
start_btn.pack(pady=10)

progress = ttk.Progressbar(root, length=520)
progress.pack(pady=5)

tk.Label(root, textvariable=status_var).pack()

log_box = tk.Text(root, height=18, font=("Consolas", 9))
log_box.pack(fill="both", expand=True, padx=10, pady=10)

root.after(100, poll_queues)
root.mainloop()
