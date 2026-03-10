#!/usr/bin/env python3
"""
小红书下载器 — 配合本地相册查看器 (local-ig.html) 使用

安装依赖（只需一次）:
    pip install xhs requests

用法:
    python xhs_download.py <用户主页URL或user_id>
    python xhs_download.py <user_id> --count 50
    python xhs_download.py <user_id> --output D:/photos
    python xhs_download.py <user_id> --cookie "your_cookie_string"

下载完成后:
    1. 用 Chrome / Edge 打开 local-ig.html
    2. 点「选择文件夹」，选中 downloads/<user_id> 目录
    3. 所有笔记自动导入，文案、时间、地点全部就位
"""

import json
import os
import sys
import time
import random
import platform
import subprocess
import argparse
import re
import shutil
from pathlib import Path
from datetime import datetime


COOKIE_FILE = Path(__file__).resolve().parent / 'xhs_cookie.txt'


def open_folder(path: Path):
    """在系统文件管理器中打开文件夹（跨平台）。"""
    try:
        system = platform.system()
        if system == 'Windows':
            os.startfile(str(path))
        elif system == 'Darwin':
            subprocess.run(['open', str(path)], check=True)
        else:
            subprocess.run(['xdg-open', str(path)], check=True)
    except Exception:
        pass


def _require(pkg, install_name=None):
    try:
        return __import__(pkg)
    except ImportError:
        name = install_name or pkg
        print(f"\n❌  缺少依赖包: {name}")
        print(f"    请运行: pip install {name}\n")
        sys.exit(1)


def load_cookie():
    """从文件加载 Cookie；若不存在则引导用户粘贴。"""
    if COOKIE_FILE.exists():
        cookie = COOKIE_FILE.read_text(encoding='utf-8').strip()
        if cookie:
            print(f"✓  已加载 Cookie（来自 {COOKIE_FILE.name}）")
            return cookie

    print("""
╔══════════════════════════════════════════════════════════════╗
║              获取小红书 Cookie 的步骤                        ║
╠══════════════════════════════════════════════════════════════╣
║  1. 用 Chrome/Edge 打开 https://www.xiaohongshu.com         ║
║  2. 登录你的账号                                             ║
║  3. 按 F12 打开开发者工具 → 点「Network」选项卡              ║
║  4. 刷新页面，点击任意一个请求                               ║
║  5. 在「Request Headers」中找到「cookie」行                  ║
║  6. 复制整行 cookie 的值（一长串文字）                       ║
╚══════════════════════════════════════════════════════════════╝
""")
    cookie = input("请粘贴 Cookie 值（直接回车退出）: ").strip()
    if not cookie:
        print("❌  未输入 Cookie，退出")
        sys.exit(1)

    COOKIE_FILE.write_text(cookie, encoding='utf-8')
    print(f"✓  Cookie 已保存到 {COOKIE_FILE.name}，下次无需重新输入\n")
    return cookie


def parse_user_id(user_input: str) -> str:
    """从 URL 或直接的 user_id 字符串中提取用户 ID。"""
    # 小红书用户主页 URL: https://www.xiaohongshu.com/user/profile/<id>
    m = re.search(r'/user/profile/([0-9a-fA-F]{20,})', user_input)
    if m:
        return m.group(1)
    return user_input.strip()


# ── 下载工具 ──────────────────────────────────────────────────────────────────
XHS_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) '
        'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1'
    ),
    'Referer': 'https://www.xiaohongshu.com/',
    'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
}


def download_file(url, path, session, stream=False):
    r = session.get(url, headers=XHS_HEADERS, timeout=60, stream=stream)
    r.raise_for_status()
    if stream:
        size = 0
        with open(path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    size += len(chunk)
        return size
    else:
        data = r.content
        path.write_bytes(data)
        return len(data)


# ── 单条笔记处理 ──────────────────────────────────────────────────────────────
def process_note(client, note_brief, profile_dir, session, delay):
    """获取笔记详情、下载媒体文件、写入 meta.json。"""
    note_id = note_brief.get('note_id') or note_brief.get('id', '')

    # 获取笔记详情
    try:
        note = client.get_note_by_id(note_id)
    except Exception as e:
        err = str(e)
        print(f"  ↳ 获取详情失败: {err}")
        if '401' in err or '未登录' in err or 'login' in err.lower():
            print(f"\n  💡 Cookie 已失效，请删除 {COOKIE_FILE.name} 后重新运行")
            COOKIE_FILE.unlink(missing_ok=True)
            sys.exit(1)
        return 'fail'

    # 时间戳（毫秒） → 日期
    ts_ms = note.get('time', 0) or note_brief.get('time', 0)
    if ts_ms:
        dt = datetime.fromtimestamp(ts_ms / 1000)
        date_str = dt.strftime('%Y-%m-%d')
        date_iso = dt.isoformat()
    else:
        dt = datetime.now()
        date_str = dt.strftime('%Y-%m-%d')
        date_iso = dt.isoformat()

    folder_name = f"{date_str}_{note_id}"
    post_dir = profile_dir / folder_name

    # 已下载则跳过
    if (post_dir / 'meta.json').exists():
        print(f"  ↳ 已存在，跳过")
        return 'skip'

    post_dir.mkdir(parents=True, exist_ok=True)

    # 文案：title + desc
    title   = (note.get('title') or '').strip()
    desc    = (note.get('desc')  or '').strip()
    caption = (title + '\n' + desc).strip() if (title and desc) else (title or desc)

    # 位置
    loc_info = note.get('location') or {}
    if isinstance(loc_info, dict):
        location_name = loc_info.get('name', '')
    else:
        location_name = str(loc_info) if loc_info else ''

    # 笔记类型
    is_video = (note.get('type') == 'video')

    # 收集媒体 URL
    image_urls = []
    video_url  = None

    if is_video:
        # 视频 URL（尝试多条路径）
        try:
            streams = note['video']['media']['stream']
            for quality in ('h264', 'h265', 'av1'):
                lst = streams.get(quality) or []
                if lst:
                    video_url = lst[0].get('master_url') or lst[0].get('url', '')
                    if video_url:
                        break
        except (KeyError, TypeError):
            pass

        # 封面图
        for img in (note.get('image_list') or []):
            url = _extract_img_url(img)
            if url:
                image_urls.append(url)
                break  # 视频只取第一张封面
        if not image_urls:
            cover_url = (note.get('cover') or {}).get('url', '')
            if cover_url:
                image_urls.append(cover_url)
    else:
        # 图文：全部图片
        for img in (note.get('image_list') or []):
            url = _extract_img_url(img)
            if url:
                image_urls.append(url)

    if not image_urls and not video_url:
        shutil.rmtree(post_dir, ignore_errors=True)
        print(f"  ↳ 无可用媒体，跳过")
        return 'skip'

    # 下载封面图 / 图文图片
    downloaded_imgs = 0
    for i, url in enumerate(image_urls, 1):
        try:
            size = download_file(url, post_dir / f"{i}.jpg", session)
            kb = size // 1024
            end = '\r' if i < len(image_urls) else '\n'
            print(f"  ↳ 图片 {i}/{len(image_urls)}  ({kb} KB)", end=end, flush=True)
            downloaded_imgs += 1
            if i < len(image_urls):
                time.sleep(delay * 0.2)
        except Exception as e:
            print(f"  ↳ 图片 {i} 失败: {e}")

    # 下载视频（串流，避免大文件占满内存）
    if video_url:
        try:
            size = download_file(video_url, post_dir / '1.mp4', session, stream=True)
            print(f"  ↳ 视频  ({size // 1024} KB)")
        except Exception as e:
            print(f"  ↳ 视频下载失败: {e}")

    # 什么都没下载成功
    if downloaded_imgs == 0 and not (post_dir / '1.mp4').exists():
        shutil.rmtree(post_dir, ignore_errors=True)
        return 'fail'

    # 写入 meta.json（字段与 ig_download.py 完全对应）
    meta = {
        'caption':     caption,
        'date':        date_iso,
        'location':    location_name,
        'shortcode':   note_id,
        'ig_url':      f'https://www.xiaohongshu.com/explore/{note_id}',
        'is_video':    is_video,
        'image_count': len(image_urls),
        'source':      'xhs',
    }
    (post_dir / 'meta.json').write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )

    preview = caption.split('\n')[0][:72] if caption else '（无文案）'
    if len(caption) > 72:
        preview += '…'
    print(f"  ✓ {preview}")
    return 'ok'


def _extract_img_url(img: dict) -> str:
    """从图片对象中提取 URL（兼容多种字段名）。"""
    for key in ('url', 'url_default', 'original_url'):
        if img.get(key):
            return img[key]
    # 部分版本使用 info_list（按尺寸降序）
    info_list = img.get('info_list') or []
    if info_list:
        return info_list[-1].get('url', '')
    return ''


# ── 分页获取笔记列表 ──────────────────────────────────────────────────────────
def get_all_notes(client, user_id, limit=None):
    notes  = []
    cursor = ''
    page   = 1

    while True:
        try:
            result = client.get_user_notes(user_id, cursor=cursor)
        except Exception as e:
            err = str(e)
            print(f"\n  获取第 {page} 页失败: {err}")
            if '401' in err or '未登录' in err or 'login' in err.lower():
                print(f"\n  💡 Cookie 已失效，请删除 {COOKIE_FILE.name} 后重新运行")
                COOKIE_FILE.unlink(missing_ok=True)
                sys.exit(1)
            break

        page_notes = result.get('notes') or []
        notes.extend(page_notes)
        print(f"  → 已获取 {len(notes)} 条…", end='\r', flush=True)

        if limit and len(notes) >= limit:
            notes = notes[:limit]
            break

        has_more = result.get('has_more', False)
        cursor   = result.get('cursor', '')
        if not has_more or not cursor:
            break

        page += 1
        time.sleep(random.uniform(1.0, 2.0))

    print(f"  → 共获取 {len(notes)} 条笔记      ")
    return notes


# ── 主函数 ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description='小红书下载器，配合本地相册查看器 (local-ig.html) 使用',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('user',
        help='小红书用户主页 URL 或 user_id')
    parser.add_argument('--count', type=int, metavar='N',
        help='最多下载 N 条最新笔记（默认全部）')
    parser.add_argument('--output',
        default=str(Path(__file__).resolve().parent / 'downloads'),
        metavar='DIR',
        help='下载根目录（默认: 脚本所在目录/downloads）')
    parser.add_argument('--delay', type=float, default=2.0, metavar='SEC',
        help='笔记间隔秒数，降低限流概率（默认: 2.0）')
    parser.add_argument('--cookie', metavar='COOKIE',
        help='直接传入 Cookie 字符串（优先于保存的文件）')
    args = parser.parse_args()

    _require('xhs')
    _require('requests')

    from xhs import XhsClient
    import requests as req_module

    user_id     = parse_user_id(args.user)
    output_dir  = Path(args.output)
    profile_dir = output_dir / user_id
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / '_path.txt').write_text(
        str(profile_dir.resolve()).replace('\\', '/'),
        encoding='utf-8',
    )

    print(f"\n{'─'*48}")
    print(f"  📕  本地相册 · 小红书下载器")
    print(f"{'─'*48}")
    print(f"  目标用户: {user_id}")
    print(f"  保存位置: {profile_dir.resolve()}")
    print(f"{'─'*48}\n")

    cookie = args.cookie.strip() if args.cookie else load_cookie()
    client  = XhsClient(cookie=cookie)
    session = req_module.Session()

    # 获取笔记列表
    print("🔍  获取笔记列表…")
    notes = get_all_notes(client, user_id, limit=args.count)

    if not notes:
        print("⚠️  未获取到任何笔记，请检查 user_id 或 Cookie 是否正确")
        sys.exit(0)

    total = len(notes)
    print(f"✓  准备下载 {total} 条笔记\n")

    done = skip = fail = 0
    last_result = 'ok'

    for i, note_brief in enumerate(notes, 1):
        note_id   = note_brief.get('note_id') or note_brief.get('id', '?')
        note_type = note_brief.get('type', 'normal')
        ts_ms     = note_brief.get('time', 0)
        date_hint = datetime.fromtimestamp(ts_ms / 1000).strftime('%Y-%m-%d') if ts_ms else '?'

        print(f"[{i}/{total}]  {date_hint}  {note_id}  {'🎬' if note_type == 'video' else '🖼️ '}")

        try:
            last_result = process_note(client, note_brief, profile_dir, session, args.delay)
            if last_result == 'ok':
                done += 1
            elif last_result == 'skip':
                skip += 1
            else:
                fail += 1
        except KeyboardInterrupt:
            print("\n\n⚠️  手动中断")
            break
        except Exception as e:
            print(f"  ✗  意外错误: {e}")
            fail += 1
            last_result = 'fail'

        if last_result != 'skip' and i < total:
            jitter = random.uniform(0, args.delay * 0.5)
            time.sleep(args.delay + jitter)

    resolved = profile_dir.resolve()

    print(f"\n{'─'*48}")
    print(f"  ✅  完成  下载 {done} | 已有 {skip} | 失败 {fail}")
    print(f"{'─'*48}")
    print(f"\n  📂  文件夹已自动打开: {resolved}")
    print(f"\n  📱  在本地相册中查看:")
    print(f"      1. 用 Chrome / Edge 打开 local-ig.html")
    print(f"      2. 点「选择文件夹」，选择上方目录")
    print()

    open_folder(resolved)


if __name__ == '__main__':
    main()
