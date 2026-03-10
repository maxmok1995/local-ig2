#!/usr/bin/env python3
"""
哼哼猫 → 本地相册转换器

将哼哼猫下载的文件夹结构转换成 local-ig.html 兼容格式。

哼哼猫原始结构:
    mao/
      juffthanarak/
        1_{文案}/
          圖片.jpg  圖片(1).jpg  圖片(2).jpg  視頻.mp4
        2_{文案}/
          ...

转换后结构（in-place）:
    mao/
      juffthanarak/
        01_{文案}/
          1.jpg  2.jpg  3.jpg  meta.json
        02_{文案}/
          1.jpg  1.mp4  meta.json   ← 视频帖，1.jpg 是封面

用法:
    python hhcat_convert.py mao/juffthanarak       # 转换单个用户文件夹
    python hhcat_convert.py mao/juffthanarak --dry-run   # 预览，不实际修改
    python hhcat_convert.py mao                    # 批量转换 mao 下所有用户
"""

import re
import os
import sys
import json
import shutil
import argparse
from pathlib import Path

# ── 工具函数 ──────────────────────────────────────────────────────────────────

IMG_EXT   = re.compile(r'\.(jpe?g|png|webp|heic|gif)$', re.IGNORECASE)
VIDEO_EXT = re.compile(r'\.(mp4|mov|avi|mkv|webm)$', re.IGNORECASE)

# 哼哼猫的图片命名：圖片.jpg / 圖片(1).jpg / 圖片(2).jpg ...
# 也兼容 图片.jpg（简体）及 image.jpg / image(1).jpg 等英文名
_IMG_BASE   = re.compile(r'^(.+?)(?:\((\d+)\))?(\.[^.]+)$')


def parse_img_index(name: str) -> int:
    """
    圖片.jpg    → 0
    圖片(1).jpg → 1
    圖片(2).jpg → 2
    """
    m = _IMG_BASE.match(name)
    if m:
        n = m.group(2)
        return int(n) if n is not None else 0
    return 0


def sort_images(names: list[str]) -> list[str]:
    """按哼哼猫命名规律排序图片文件名（圖片.jpg 排第一）。"""
    return sorted(names, key=parse_img_index)


# ── 文件夹名解析 ──────────────────────────────────────────────────────────────

POST_FOLDER = re.compile(r'^(\d+)_(.+)$')  # {序号}_{文案}


def parse_post_folder(name: str):
    """返回 (序号:int, 文案:str) 或 None（不是帖子文件夹）。"""
    m = POST_FOLDER.match(name)
    if m:
        return int(m.group(1)), m.group(2).strip()
    return None


# ── 单个帖子文件夹转换 ────────────────────────────────────────────────────────

def convert_post(post_dir: Path, caption: str, dry_run: bool) -> dict:
    """
    转换一个帖子文件夹：
    - 对图片重命名为 1.jpg, 2.jpg ...
    - 对视频重命名为 1.mp4（并将对应封面作为 1.jpg）
    - 写入 meta.json
    返回操作摘要 dict。
    """
    entries = list(post_dir.iterdir())
    imgs   = sorted([f for f in entries if f.is_file() and IMG_EXT.search(f.suffix)],
                    key=lambda f: parse_img_index(f.name))
    videos = sorted([f for f in entries if f.is_file() and VIDEO_EXT.search(f.suffix)],
                    key=lambda f: parse_img_index(f.name))

    renames = []   # list of (src_path, dst_path)

    if videos:
        # 视频帖：视频 → 1.mp4
        vid = videos[0]
        dst_vid = post_dir / '1.mp4'
        if vid.name != '1.mp4':
            renames.append((vid, dst_vid))

        # 图片（封面或混合媒体中的内容图）依次编号：1.jpg, 2.jpg …
        for i, img in enumerate(imgs, 1):
            ext = img.suffix.lower()
            if ext == '.jpeg':
                ext = '.jpg'
            dst = post_dir / f"{i}{ext}"
            if img.name != dst.name:
                renames.append((img, dst))

        is_video = True
        image_count = len(imgs)   # 0 = 无封面，1+ = 封面或混合内容
    else:
        # 图片帖：依次编号
        is_video = False
        image_count = len(imgs)
        for i, img in enumerate(imgs, 1):
            ext = img.suffix.lower()
            if ext == '.jpeg':
                ext = '.jpg'
            dst = post_dir / f"{i}{ext}"
            if img.name != dst.name:
                renames.append((img, dst))

    # 写 meta.json（如果已存在则跳过）
    meta_path = post_dir / 'meta.json'
    meta = {
        'caption':     caption,
        'date':        None,         # 哼哼猫不含日期，HTML 会自动用文件修改时间
        'location':    '',
        'location_id': '',
        'shortcode':   '',
        'ig_url':      '',
        'is_video':    is_video,
        'image_count': image_count,
        'downloaded':  image_count if not is_video else (image_count + (1 if videos else 0)),
        'source':      'hhcat',
    }

    # ── 执行 ──────────────────────────────────────────────────────────────────
    action_log = []
    if not dry_run:
        # 两阶段重命名：先统一改为临时名，再改为最终名，防止同目录内目标已存在时被静默覆盖
        tmp_renames = []
        for src, dst in renames:
            if dst is None:
                src.unlink(missing_ok=True)
                action_log.append(f"  删除: {src.name}")
            else:
                tmp = src.with_name('.__hhcat_tmp__' + src.name)
                src.rename(tmp)
                tmp_renames.append((tmp, dst))
                action_log.append(f"  {src.name} → {dst.name}")
        for tmp, dst in tmp_renames:
            tmp.rename(dst)
        if not meta_path.exists():
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
            action_log.append(f"  写入 meta.json")
    else:
        for src, dst in renames:
            if dst is None:
                action_log.append(f"  [删除] {src.name}")
            else:
                action_log.append(f"  [重命名] {src.name} → {dst.name}")
        if not meta_path.exists():
            action_log.append(f"  [写入] meta.json  caption={caption[:40]}")

    return {
        'renames': len([r for r in renames if r[1] is not None]),
        'deletes': len([r for r in renames if r[1] is None]),
        'meta':    not meta_path.exists() or dry_run,
        'log':     action_log,
        'is_video': is_video,
        'img_count': image_count,
    }


# ── 用户文件夹转换 ────────────────────────────────────────────────────────────

def convert_user(user_dir: Path, dry_run: bool):
    """转换一个用户文件夹下所有帖子。"""
    posts = []
    for child in user_dir.iterdir():
        if child.is_dir() and not child.name.startswith('.'):
            parsed = parse_post_folder(child.name)
            if parsed:
                posts.append((parsed[0], parsed[1], child))

    if not posts:
        print(f"  ⚠️  未找到符合格式的帖子文件夹（期望格式：{{序号}}_{{文案}}）")
        return

    # 按序号排序
    posts.sort(key=lambda x: x[0])
    total = len(posts)
    pad = len(str(total))  # 零填充位数

    print(f"  找到 {total} 个帖子文件夹")
    print()

    done = skipped = 0
    for num, caption, post_dir in posts:
        # 零填充后的新文件夹名（使 local-ig.html 能正确排序）
        new_name = f"{str(num).zfill(pad)}_{caption}"
        new_dir  = user_dir / new_name

        # 先转换内部文件（在原目录上操作）
        result = convert_post(post_dir, caption, dry_run)

        if result['log'] or post_dir.name != new_name:
            tag = '📹' if result['is_video'] else f"🖼️ ×{result['img_count']}"
            print(f"[{num}] {tag}  {caption[:50]}")
            for line in result['log']:
                print(line)
            # 重命名文件夹（序号零填充）
            if post_dir.name != new_name:
                if dry_run:
                    print(f"  [重命名文件夹] {post_dir.name} → {new_name}")
                else:
                    if new_dir.exists():
                        print(f"  ⚠️  目标文件夹已存在，跳过重命名: {new_name}")
                    else:
                        post_dir.rename(new_dir)
                        print(f"  文件夹 → {new_name}")
            done += 1
        else:
            skipped += 1

    # 写 _path.txt（供 HTML 显示绝对路径）
    path_file = user_dir / '_path.txt'
    if not path_file.exists():
        abs_path = str(user_dir.resolve()).replace('\\', '/')
        if dry_run:
            print(f"\n[写入] _path.txt → {abs_path}")
        else:
            path_file.write_text(abs_path, encoding='utf-8')
            print(f"\n写入 _path.txt")

    print()
    print(f"  ✅  完成  处理 {done} 个 | 已转换跳过 {skipped} 个")


# ── 主函数 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='哼哼猫下载文件夹 → local-ig.html 兼容格式转换器',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('path',
        help='哼哼猫下载的用户文件夹路径（如 mao/juffthanarak）'
             '，或包含多个用户的根目录（如 mao/）')
    parser.add_argument('--dry-run', action='store_true',
        help='预览模式：只显示将要执行的操作，不实际修改文件')
    args = parser.parse_args()

    target = Path(args.path).resolve()
    if not target.exists():
        print(f"❌  路径不存在: {target}")
        sys.exit(1)

    dry = args.dry_run
    if dry:
        print(f"\n{'─'*52}")
        print(f"  🔍  预览模式（dry-run），不会修改任何文件")
        print(f"{'─'*52}\n")

    # 判断是单用户文件夹还是根目录
    # 如果目录下有子目录且子目录里包含 {数字}_ 命名的文件夹 → 单用户模式
    # 如果目录下的子目录本身又包含 {数字}_ 命名的文件夹 → 根目录模式（批量）

    def is_user_dir(d: Path) -> bool:
        """目录下有 {数字}_{文案} 子文件夹 → 是用户目录。"""
        return any(
            parse_post_folder(c.name) is not None
            for c in d.iterdir()
            if c.is_dir() and not c.name.startswith('.')
        )

    if is_user_dir(target):
        # 单用户模式
        print(f"\n{'─'*52}")
        print(f"  👤  用户目录: {target.name}")
        print(f"{'─'*52}\n")
        convert_user(target, dry)
    else:
        # 根目录模式：遍历子目录
        user_dirs = [d for d in target.iterdir() if d.is_dir() and not d.name.startswith('.')]
        user_dirs = [d for d in user_dirs if is_user_dir(d)]
        if not user_dirs:
            print(f"❌  在 {target} 下未找到符合格式的用户文件夹")
            sys.exit(1)
        for ud in sorted(user_dirs):
            print(f"\n{'─'*52}")
            print(f"  👤  用户目录: {ud.name}")
            print(f"{'─'*52}\n")
            convert_user(ud, dry)

    print()
    if not dry:
        print(f"  📱  下一步：")
        print(f"      1. 用 Chrome / Edge 打开 local-ig.html")
        print(f"      2. 点「选择本地照片文件夹」→ 选中 {target.name} 目录")
    print()


if __name__ == '__main__':
    main()
