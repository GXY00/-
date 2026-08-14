# -*- coding: utf-8 -*-
"""
中文字幕 / 外文字幕 时间轴自动对齐工具

功能：
  1. 打开 .lrc / .txt 等字幕歌词文件（自动识别 UTF-8 / GBK 等编码）
  2. 选择从第几行开始对齐
  3. 自动识别中文行与外文行（韩文/英文/日文等），
     把中文歌词的时间轴对齐到对应的外文歌词时间轴
  4. 表格预览对齐结果，并另存为新文件

对齐示例：
  对齐前:
    [00:14.750]捎在热风中的
    [00:14.754]뜨거운 바람에 실린
  对齐后:
    [00:14.754]捎在热风中的
    [00:14.754]뜨거운 바람에 실린

孤立行示例（无需对齐的外文行会保持原样）：
    [00:33.082]你的气场隐约可见
    [00:33.082]아른거려 너의 Aura
    [00:34.660]Phew phew          <- 孤立外文，无人认领，保持不变
    [00:35.248]我的心再次变得滚烫
    [00:35.248]다시 뜨거워져 내 맘

运行方式：python 字幕对齐工具.py
"""

import os
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

# ---------------------------------------------------------------- 解析部分

# 匹配行首的时间戳，例如 [00:14.750]、[01:02.03]、[2:34]
TIMESTAMP_RE = re.compile(r'\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]')

# 匹配中文汉字（含扩展区）
CJK_RE = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]')


def split_line(raw: str):
    """把一行拆成 (时间戳前缀, 时间戳后的剩余文本, 文本内容)。

    - ts_prefix：行首连续的所有 [mm:ss.xx] 时间戳
    - suffix：时间戳之后剩余部分（原样保留，用于拼接回写）
    - text：suffix 去空格后的内容，用于判断中英文
    """
    line = raw.rstrip('\r\n')
    pos = 0
    ts_prefix = ''
    while pos < len(line):
        m = TIMESTAMP_RE.match(line, pos)
        if not m:
            break
        ts_prefix += m.group(0)
        pos = m.end()
    suffix = line[pos:]
    text = suffix.strip()
    return ts_prefix, suffix, text


def read_text(path: str) -> str:
    """按常见编码顺序读取文件内容。"""
    for enc in ('utf-8-sig', 'utf-8', 'gb18030', 'latin-1'):
        try:
            with open(path, 'r', encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        return f.read()


def is_chinese(text: str) -> bool:
    """判断文本是否为中文（含汉字即视为中文行）。"""
    return bool(CJK_RE.search(text))


def first_timestamp(ts_prefix: str) -> str:
    """取出时间戳前缀里的第一个时间戳，用于表格展示。"""
    m = TIMESTAMP_RE.search(ts_prefix)
    return m.group(0) if m else ''


def parse_time_ms(ts_prefix: str):
    """把时间戳前缀解析成毫秒数（取第一个时间戳）。无法解析返回 None。"""
    m = TIMESTAMP_RE.search(ts_prefix)
    if not m:
        return None
    mm = int(m.group(1))
    ss = int(m.group(2))
    frac = m.group(3) or '0'
    # 小数部分：1 位按十分之一秒、2 位按百分秒、3 位按毫秒换算
    if len(frac) == 1:
        ms = int(frac) * 100
    elif len(frac) == 2:
        ms = int(frac) * 10
    else:
        ms = int(frac[:3])
    return ((mm * 60 + ss) * 1000) + ms


def build_model(raw_lines):
    """从原始行构建内容模型。

    只把「带时间戳且带文字」的行当作内容行，其余（元数据 [ti:]、空行等）原样保留。
    返回 content 列表，每项为 dict:
        raw_idx     在原文件中的行号（0 开始）
        ts_prefix   当前时间戳前缀（对齐过程中会被修改）
        orig_ts_prefix  原始时间戳前缀（用于复位/展示原时间）
        suffix      时间戳后的剩余文本
        text        去除空格后的文本
    """
    content = []
    for idx, raw in enumerate(raw_lines):
        ts_prefix, suffix, text = split_line(raw)
        if ts_prefix and text:
            content.append({
                'raw_idx': idx,
                'ts_prefix': ts_prefix,
                'orig_ts_prefix': ts_prefix,
                'suffix': suffix,
                'text': text,
            })
    return content


def detect_mode(content, start_idx):
    """自动检测中外的排列方式。

    返回 'chinese_first'（中文行在前、外文行在后）或
        'foreign_first'（外文行在前、中文行在后）。
    """
    n = len(content)
    for i in range(start_idx, n - 1):
        a_ch = is_chinese(content[i]['text'])
        b_ch = is_chinese(content[i + 1]['text'])
        if a_ch and not b_ch:
            return 'chinese_first'
        if b_ch and not a_ch:
            return 'foreign_first'
    return 'chinese_first'


def _new_result(mode):
    return {
        'changes': [],          # [(raw_idx, 原时间前缀, 新时间前缀), ...]
        'matched_cn': set(),    # 已配对的中文行 raw_idx
        'unmatched_cn': set(),  # 未找到对应外文的中文行 raw_idx
        'claimed_foreign': set(),    # 已被认领的外文行 raw_idx
        'unclaimed_foreign': set(),  # 未被认领（孤立）的外文行 raw_idx
        'mode': mode,
    }


def align_by_time(content, start_idx, threshold_ms=1000.0):
    """按时间戳接近度配对（推荐）。

    每句中文对齐到「时间最接近、且未被认领」的外文行；若最小时间差超过
    threshold_ms，则判定该中文行无对应翻译，跳过。
    这样孤立的外文行（如纯拟声词、无翻译的歌词）不会被任何中文认领，保持原样。

    返回结果 dict（见 _new_result）。
    """
    res = _new_result('time')

    cn_list = []
    fg_list = []
    for i in range(start_idx, len(content)):
        it = content[i]
        t = parse_time_ms(it['orig_ts_prefix'])
        if is_chinese(it['text']):
            cn_list.append((i, t))
        elif t is not None:
            fg_list.append((i, t))

    for ci, ct in cn_list:
        it = content[ci]
        if ct is None:
            res['unmatched_cn'].add(it['raw_idx'])
            continue

        best = None
        best_d = None
        for fi, ft in fg_list:
            if content[fi]['raw_idx'] in res['claimed_foreign'] or ft is None:
                continue
            d = abs(ct - ft)
            if best_d is None or d < best_d:
                best_d = d
                best = fi

        if best is not None and best_d <= threshold_ms:
            res['claimed_foreign'].add(content[best]['raw_idx'])
            new_prefix = content[best]['ts_prefix']
            if it['ts_prefix'] != new_prefix:
                it['ts_prefix'] = new_prefix
                res['changes'].append((it['raw_idx'], it['orig_ts_prefix'], new_prefix))
            res['matched_cn'].add(it['raw_idx'])
        else:
            res['unmatched_cn'].add(it['raw_idx'])

    res['unclaimed_foreign'] = {
        content[fi]['raw_idx']
        for fi, ft in fg_list
        if content[fi]['raw_idx'] not in res['claimed_foreign']
    }
    return res


def align_times(content, start_idx, mode='auto'):
    """按位置配对（兼容旧策略）。

    mode:
        'auto'           自动检测中外排列
        'chinese_first'  中文在前：中文行取后面最近的一行外文
        'foreign_first'  外文在前：中文行取前面最近的一行外文

    返回结果 dict（见 _new_result）。
    """
    if mode == 'auto':
        mode = detect_mode(content, start_idx)
    res = _new_result(mode)
    n = len(content)

    for i in range(start_idx, n):
        it = content[i]
        if not is_chinese(it['text']):
            continue

        target = None
        if mode == 'chinese_first':
            for j in range(i + 1, n):
                if not is_chinese(content[j]['text']):
                    target = j
                    break
        else:
            for j in range(i - 1, -1, -1):
                if not is_chinese(content[j]['text']):
                    target = j
                    break

        if target is None:
            res['unmatched_cn'].add(it['raw_idx'])
            continue

        res['claimed_foreign'].add(content[target]['raw_idx'])
        old = it['ts_prefix']
        new = content[target]['ts_prefix']
        if old != new:
            it['ts_prefix'] = new
            res['changes'].append((it['raw_idx'], old, new))
        res['matched_cn'].add(it['raw_idx'])

    for j in range(start_idx, n):
        fj = content[j]
        if not is_chinese(fj['text']) and fj['raw_idx'] not in res['claimed_foreign']:
            res['unclaimed_foreign'].add(fj['raw_idx'])

    return res


# ---------------------------------------------------------------- 界面部分

MODE_LABELS = {
    'time': '时间匹配（推荐）',
    'chinese_first': '中文在前',
    'foreign_first': '外文在前',
}


class AlignApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title('中文字幕 · 外文字幕 时间轴对齐工具')
        self.root.geometry('980x680')
        self.root.minsize(780, 520)

        self.file_path = None
        self.raw_lines = []
        self.content = []
        self.content_by_raw = {}
        self.last_result = None

        self.mode_var = tk.StringVar(value='time')
        self.start_line_var = tk.StringVar(value='1')
        self.threshold_var = tk.StringVar(value='1.0')
        self.status_var = tk.StringVar(value='请先选择字幕文件')

        self._build_ui()

    # ---------------- 界面构建 ----------------
    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except Exception:
            pass
        default_font = ('Microsoft YaHei UI', 10)
        self.root.option_add('*Font', default_font)

        # 顶部工具条
        top = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        top.pack(side='top', fill='x')

        self.btn_open = ttk.Button(top, text='选择字幕文件…', command=self.on_open)
        self.btn_open.pack(side='left')

        self.file_label = ttk.Label(top, text='（未选择文件）', foreground='#555555')
        self.file_label.pack(side='left', padx=(12, 0))

        self.btn_help = ttk.Button(top, text='使用说明', command=self.on_help)
        self.btn_help.pack(side='right')

        # 选项区
        opts = ttk.Frame(self.root, padding=(10, 0, 10, 4))
        opts.pack(side='top', fill='x')

        ttk.Label(opts, text='从第几行开始对齐：').pack(side='left')
        self.entry_start = ttk.Entry(opts, textvariable=self.start_line_var, width=8)
        self.entry_start.pack(side='left')

        ttk.Label(opts, text='  对齐方式：').pack(side='left', padx=(16, 0))
        for key, label in MODE_LABELS.items():
            ttk.Radiobutton(opts, text=label, variable=self.mode_var, value=key).pack(side='left', padx=(0, 6))

        ttk.Label(opts, text='  最大时间差(秒)：').pack(side='left', padx=(16, 0))
        self.entry_threshold = ttk.Entry(opts, textvariable=self.threshold_var, width=6)
        self.entry_threshold.pack(side='left')

        self.btn_align = ttk.Button(opts, text='开始对齐', command=self.on_align)
        self.btn_align.pack(side='left', padx=(16, 0))

        self.btn_save = ttk.Button(opts, text='保存结果…', command=self.on_save)
        self.btn_save.pack(side='left', padx=(8, 0))

        # 预览表格
        mid = ttk.Frame(self.root, padding=(10, 4, 10, 4))
        mid.pack(side='top', fill='both', expand=True)

        columns = ('lineno', 'orig_time', 'new_time', 'text')
        self.tree = ttk.Treeview(mid, columns=columns, show='headings', selectmode='browse')
        self.tree.heading('lineno', text='行号')
        self.tree.heading('orig_time', text='原时间')
        self.tree.heading('new_time', text='对齐后时间')
        self.tree.heading('text', text='歌词内容')

        self.tree.column('lineno', width=70, anchor='e', stretch=False)
        self.tree.column('orig_time', width=110, anchor='center', stretch=False)
        self.tree.column('new_time', width=110, anchor='center', stretch=False)
        self.tree.column('text', width=640, anchor='w')

        vsb = ttk.Scrollbar(mid, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)

        self.tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)

        # 行标签颜色
        self.tree.tag_configure('cn', background='#fff4d6')
        self.tree.tag_configure('aligned', background='#d8f0dd', foreground='#1c7a2e')
        self.tree.tag_configure('unmatched', background='#fde2e2', foreground='#b02a2a')
        self.tree.tag_configure('foreign', background='#e8eefc')
        self.tree.tag_configure('orphan', background='#f0f0f0', foreground='#888888')
        self.tree.tag_configure('meta', foreground='#9a9a9a')

        # 状态栏
        bottom = ttk.Frame(self.root, padding=(10, 4, 10, 8))
        bottom.pack(side='bottom', fill='x')
        ttk.Label(bottom, textvariable=self.status_var).pack(side='left')

    # ---------------- 事件处理 ----------------
    def on_open(self):
        path = filedialog.askopenfilename(
            title='选择字幕文件',
            filetypes=[('字幕/歌词文件', '*.lrc *.txt *.srt'), ('LRC 歌词', '*.lrc'),
                       ('文本文件', '*.txt'), ('所有文件', '*.*')],
        )
        if not path:
            return
        self.file_path = path
        self.file_label.config(text=os.path.basename(path))

        try:
            text = read_text(path)
        except OSError as e:
            messagebox.showerror('读取失败', f'无法读取文件：\n{e}')
            return

        self.raw_lines = text.splitlines()
        self.content = build_model(self.raw_lines)
        self.content_by_raw = {item['raw_idx']: item for item in self.content}
        self.last_result = None

        # 复位起始行到第一个中文内容行
        suggest = 1
        for item in self.content:
            if is_chinese(item['text']):
                suggest = item['raw_idx'] + 1
                break
        self.start_line_var.set(str(suggest))

        self.refresh_tree()
        self.status_var.set(
            f'已载入 {len(self.raw_lines)} 行，其中内容行 {len(self.content)} 行'
            f'（中文 {sum(1 for i in self.content if is_chinese(i["text"]))} 行）'
        )

        # 询问用户从哪一行开始对齐
        answer = simpledialog.askinteger(
            '选择起始行',
            f'从第几行开始对齐？\n（文件共 {len(self.raw_lines)} 行，1 开始计数）',
            parent=self.root,
            initialvalue=suggest,
            minvalue=1,
            maxvalue=len(self.raw_lines),
        )
        if answer is not None:
            self.start_line_var.set(str(answer))
            # 自动滚动到该行
            self._scroll_to_line(answer)

    def _scroll_to_line(self, line_no):
        for item_id in self.tree.get_children():
            values = self.tree.item(item_id, 'values')
            if values and values[0].isdigit() and int(values[0]) == line_no:
                self.tree.see(item_id)
                self.tree.selection_set(item_id)
                break

    def _start_content_idx(self, start_line: int) -> int:
        raw_start = start_line - 1
        for i, item in enumerate(self.content):
            if item['raw_idx'] >= raw_start:
                return i
        return len(self.content)

    def on_align(self):
        if not self.content:
            messagebox.showwarning('提示', '请先选择一个字幕文件。')
            return
        try:
            start_line = int(self.start_line_var.get())
        except ValueError:
            messagebox.showwarning('提示', '起始行必须是数字。')
            return
        start_line = max(1, min(start_line, len(self.raw_lines)))

        start_idx = self._start_content_idx(start_line)
        mode = self.mode_var.get()

        threshold_ms = 1000.0
        if mode == 'time':
            try:
                threshold_ms = float(self.threshold_var.get()) * 1000.0
            except ValueError:
                threshold_ms = 1000.0
            threshold_ms = max(0.0, threshold_ms)

        # 每次对齐前复位到原始时间，避免重复对齐导致偏差
        for item in self.content:
            item['ts_prefix'] = item['orig_ts_prefix']

        if mode == 'time':
            result = align_by_time(self.content, start_idx, threshold_ms)
        else:
            result = align_times(self.content, start_idx, mode)

        self.last_result = result
        self.refresh_tree()

        mode_name = MODE_LABELS.get(result['mode'], result['mode'])
        self.status_var.set(
            f'完成：从第 {start_line} 行开始（{mode_name}），对齐 {len(result["changes"])} 行，'
            f'未匹配中文 {len(result["unmatched_cn"])} 行，'
            f'孤立外文 {len(result["unclaimed_foreign"])} 行。'
        )

    def refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        last = self.last_result
        for raw_idx, raw in enumerate(self.raw_lines):
            lineno = raw_idx + 1
            item = self.content_by_raw.get(raw_idx)
            if item is None:
                # 元数据 / 空行
                text = raw.strip()
                self.tree.insert('', 'end', values=(lineno, '', '', text), tags=('meta',))
                continue
            orig_time = first_timestamp(item['orig_ts_prefix'])
            new_time = first_timestamp(item['ts_prefix'])
            text = item['text']
            if is_chinese(text):
                if item['ts_prefix'] != item['orig_ts_prefix']:
                    tag = 'aligned'
                elif last and raw_idx in last['unmatched_cn']:
                    tag = 'unmatched'
                else:
                    tag = 'cn'
            else:
                if last and raw_idx in last['unclaimed_foreign']:
                    tag = 'orphan'
                else:
                    tag = 'foreign'
            self.tree.insert('', 'end', values=(lineno, orig_time, new_time, text), tags=(tag,))

    def on_save(self):
        if not self.raw_lines:
            messagebox.showwarning('提示', '请先选择一个字幕文件。')
            return

        out_lines = list(self.raw_lines)
        for item in self.content:
            out_lines[item['raw_idx']] = item['ts_prefix'] + item['suffix']

        default_name = os.path.splitext(os.path.basename(self.file_path))[0] + '_aligned.lrc'
        path = filedialog.asksaveasfilename(
            title='保存对齐结果',
            defaultextension='.lrc',
            initialfile=default_name,
            filetypes=[('LRC 歌词', '*.lrc'), ('文本文件', '*.txt'), ('所有文件', '*.*')],
        )
        if not path:
            return

        text = '\n'.join(out_lines)
        if self.raw_lines:
            text += '\n'
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
        except OSError as e:
            messagebox.showerror('保存失败', f'无法写入文件：\n{e}')
            return

        messagebox.showinfo('保存成功', f'已保存到：\n{path}')
        self.status_var.set(f'已保存：{path}')

    def on_help(self):
        messagebox.showinfo(
            '使用说明',
            '1. 点击「选择字幕文件…」打开 .lrc / .txt 文件。\n'
            '2. 在弹出的对话框中输入从第几行开始对齐（也可在选项区修改）。\n'
            '3. 选择对齐方式：\n'
            '     · 时间匹配（推荐）：按时间戳接近度配对，自动跳过无对应的孤立行。\n'
            '     · 中文在前：中文行下面紧跟一行外文。\n'
            '     · 外文在前：外文行下面紧跟一行中文。\n'
            '4. 点击「开始对齐」，表格中绿色行即被对齐的中文行。\n'
            '5. 确认无误后点击「保存结果…」另存为新文件。\n\n'
            '最大时间差（秒）：时间匹配模式下，中文行与外文行的时间差\n'
            '超过该值时判定为「无对应翻译」并跳过。歌词本身时间就很接近，\n'
            '默认 1.0 秒即可；若对应行时间差较大，可调大该值。\n\n'
            '颜色说明：\n'
            '· 黄色 = 中文（待对齐）  绿色 = 已对齐\n'
            '· 浅红 = 未匹配到外文的中文（跳过）\n'
            '· 蓝色 = 外文  灰色 = 孤立外文（无需对齐，保持不变）\n\n'
            '说明：\n'
            '· 元数据行（如 [ti:]、[ar:]）和空行会被原样保留。\n'
            '· 只会修改中文行的时间轴，外文行时间保持不变。\n'
            '· 孤立的外文行（如纯拟声词 Phew phew）不会被改动。',
        )


def main():
    root = tk.Tk()
    AlignApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
