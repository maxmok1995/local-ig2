#!/usr/bin/env python3
"""
Instagram 下载器 — 配合本地相册查看器 (local-ig.html) 使用

安装依赖（只需一次）:
    pip install instaloader requests

用法:
    python ig_download.py natgeo                       # 下载公开账号
    python ig_download.py natgeo --count 30            # 只下载最新 30 条
    python ig_download.py natgeo --start 201 --end 400 # 下载第 201～400 条（范围模式）
    python ig_download.py yourusername --login         # 登录后下载（私密账号或自己的号）
    python ig_download.py natgeo --output D:/photos    # 自定义保存目录
    python ig_download.py natgeo --delay 3             # 每条间隔 3 秒（降低被限流概率）

下载完成后:
    1. 用 Chrome / Edge 打开 local-ig.html
    2. 点「选择文件夹」，选中 downloads/<用户名> 目录
    3. 所有帖子自动导入，文案、时间、地点全部就位
"""

import json
import os
import sys
import time
import random
import platform
import subprocess
import argparse
import shutil
from itertools import islice
from pathlib import Path

RATE_LIMIT_WAIT    = 300  # 被限流后首次等待秒数（5 分钟）
RATE_LIMIT_RETRIES = 5    # 最多重试次数


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
        pass  # 打开失败不影响主流程


# ── 依赖检查 ──────────────────────────────────────────────────────────────────
def _require(pkg, install_name=None):
    try:
        return __import__(pkg)
    except ImportError:
        name = install_name or pkg
        print(f"\n❌  缺少依赖包: {name}")
        print(f"    请运行: pip install {name}\n")
        sys.exit(1)


# ── 图片下载 ──────────────────────────────────────────────────────────────────
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) '
        'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1'
    ),
    'Referer': 'https://www.instagram.com/',
    'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
}


def download_file(url, path, session, stream=False):
    r = session.get(url, headers=HEADERS, timeout=60, stream=stream)
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


# ── 单条帖子处理 ──────────────────────────────────────────────────────────────
def process_post(post, profile_dir, session, delay):
    date_str = post.date_local.strftime('%Y-%m-%d')
    folder_name = f"{date_str}_{post.shortcode}"
    post_dir = profile_dir / folder_name

    # 已下载则跳过
    if (post_dir / 'meta.json').exists():
        print(f"  ↳ 已存在，跳过")
        return 'skip'

    post_dir.mkdir(parents=True, exist_ok=True)

    # 收集图片 URL（单图 / 轮播 / 视频封面）及视频 URL
    urls = []
    video_urls = []   # list of (index, url)
    try:
        if post.typename == 'GraphSidecar':
            for i, node in enumerate(post.get_sidecar_nodes(), 1):
                urls.append(node.display_url)          # 封面图（含视频节点的静态封面）
                if getattr(node, 'is_video', False) and getattr(node, 'video_url', None):
                    video_urls.append((i, node.video_url))
        else:
            urls.append(post.url)   # 视频帖子 post.url 是封面图
            if post.is_video and post.video_url:
                video_urls.append((1, post.video_url))
    except Exception as e:
        print(f"  ↳ 获取图片链接失败: {e}")
        urls = [post.url] if post.url else []

    if not urls:
        shutil.rmtree(post_dir, ignore_errors=True)
        print(f"  ↳ 无可用图片，跳过")
        return 'skip'

    # 下载图片
    failed = 0
    for i, url in enumerate(urls, 1):
        try:
            size = download_file(url, post_dir / f"{i}.jpg", session)
            kb = size // 1024
            end = '\r' if i < len(urls) else '\n'
            print(f"  ↳ 图片 {i}/{len(urls)}  ({kb} KB)", end=end, flush=True)
            if i < len(urls):
                time.sleep(delay * 0.2)
        except Exception as e:
            failed += 1
            print(f"  ↳ 图片 {i} 失败: {e}")

    saved = list(post_dir.glob('*.jpg'))
    if not saved:
        shutil.rmtree(post_dir, ignore_errors=True)
        return 'fail'

    # 下载视频文件（与封面同编号，扩展名 .mp4），使用串流避免大文件占满内存
    for i, vurl in video_urls:
        try:
            size = download_file(vurl, post_dir / f"{i}.mp4", session, stream=True)
            print(f"  ↳ 视频 {i}  ({size//1024} KB)")
        except Exception as e:
            print(f"  ↳ 视频 {i} 下载失败: {e}")

    # 地点
    location_name = ''
    location_id = ''
    if post.location:
        try:
            location_name = post.location.name or ''
            location_id = str(post.location.id) if post.location.id else ''
        except Exception:
            pass

    # 文案
    caption = (post.caption or '').strip()

    # 点赞数（未登录时部分账号不可见）
    likes = None
    try:
        likes = post.likes
    except Exception:
        pass

    # 写入 meta.json
    meta = {
        'caption':      caption,
        'date':         post.date_local.isoformat(),
        'location':     location_name,
        'location_id':  location_id,
        'shortcode':    post.shortcode,
        'ig_url':       f'https://www.instagram.com/p/{post.shortcode}/',
        'is_video':     post.is_video,
        'image_count':  len(urls),
        'downloaded':   len(saved),
    }
    if likes is not None:
        meta['likes'] = likes

    (post_dir / 'meta.json').write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )

    # 预览文案（第一行，最多 72 字符）
    preview = caption.split('\n')[0][:72] if caption else '（无文案）'
    if len(caption) > 72:
        preview += '…'
    print(f"  ✓ {preview}")
    return 'ok'


# ── 主函数 ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description='Instagram 下载器，配合本地相册查看器 (local-ig.html) 使用',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('username',
        help='Instagram 用户名（不含 @）')
    parser.add_argument('--login', action='store_true',
        help='登录 Instagram（支持私密账号 / 提高请求限额）')
    parser.add_argument('--check', action='store_true',
        help='只查询帖子总数，不下载（快速确认账号可访问性）')
    parser.add_argument('--count', type=int, metavar='N',
        help='最多下载 N 条最新帖子（默认全部）；与 --start/--end 互斥')
    parser.add_argument('--start', type=int, default=None, metavar='N',
        help='范围模式：从第 N 条开始下载（1 = 最新，与 --end 配合使用）')
    parser.add_argument('--end', type=int, default=None, metavar='N',
        help='范围模式：下载到第 N 条为止（与 --start 配合使用）')
    parser.add_argument('--output', default=str(Path(__file__).resolve().parent/'downloads'), metavar='DIR',
        help='下载根目录（默认: 脚本所在目录/downloads）')
    parser.add_argument('--delay', type=float, default=2.0, metavar='SEC',
        help='帖子间隔秒数，降低限流概率（默认: 2.0）')
    args = parser.parse_args()

    # 加载依赖
    instaloader = _require('instaloader')
    requests    = _require('requests')

    # --check 模式：只查询帖数，不创建目录
    if not args.check:
        output_dir  = Path(args.output)
        profile_dir = output_dir / args.username
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / '_path.txt').write_text(
            str(profile_dir.resolve()).replace('\\', '/'),
            encoding='utf-8',
        )
        print(f"\n{'─'*48}")
        print(f"  📸  本地相册 · IG 下载器")
        print(f"{'─'*48}")
        print(f"  目标账号: @{args.username}")
        print(f"  保存位置: {profile_dir.resolve()}")
        print(f"{'─'*48}\n")
    else:
        profile_dir = None
        print(f"\n🔢  查询 @{args.username} 的帖子总数…")

    # 初始化 Instaloader（--check 模式减少重试次数，快速失败）
    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
        max_connection_attempts=1 if args.check else 3,
    )

    # 登录
    if args.login:
        import getpass
        login_user = input("你的 Instagram 用户名: ").strip()
        password   = getpass.getpass("密码（输入时不显示）: ")
        try:
            L.login(login_user, password)
            print("✓ 登录成功\n")
        except instaloader.exceptions.BadCredentialsException:
            print("❌ 密码错误，请确认后重试")
            sys.exit(1)
        except instaloader.exceptions.TwoFactorAuthRequiredException:
            code = input("请输入双重验证码: ").strip()
            try:
                L.two_factor_login(code)
                print("✓ 双重验证成功\n")
            except Exception as e:
                print(f"❌ 验证失败: {e}")
                sys.exit(1)
        except Exception as e:
            print(f"❌ 登录失败: {e}")
            sys.exit(1)

    # 获取 Profile
    print("🔍  获取账号信息…")
    try:
        profile = instaloader.Profile.from_username(L.context, args.username)
    except instaloader.exceptions.ProfileNotExistsException:
        print(f"❌  账号 @{args.username} 不存在")
        sys.exit(1)
    except instaloader.exceptions.PrivateProfileNotFollowedException:
        print(f"❌  @{args.username} 是私密账号")
        print(f"    请加 --login 参数，以关注者身份访问")
        sys.exit(1)
    except Exception as e:
        msg = str(e)
        print(f"❌  获取失败: {msg}")
        if '401' in msg or 'Unauthorized' in msg or 'wait a few minutes' in msg:
            print(f"\n  💡 Instagram 要求登录验证，请在命令末尾加上 --login 参数后重试")
        sys.exit(1)

    total = profile.mediacount

    if args.check:
        print(f"\n  ✓  @{profile.username}  共 {total} 条帖子")
        print(f"\n  → 请将 {total} 填入页面的帖数输入框，选择下载范围\n")
        sys.exit(0)

    session = requests.Session()

    if args.start is not None or args.end is not None:
        start = max(1, args.start or 1)
        end   = min(args.end or total, total)
        limit = max(0, end - start + 1)
        posts_iter = islice(profile.get_posts(), start - 1, end)
        print(f"✓  @{profile.username}  {total} 条帖子，准备下载第 {start}–{end} 条（共 {limit} 条）\n")
    else:
        limit = min(args.count, total) if args.count else total
        posts_iter = islice(profile.get_posts(), limit)
        print(f"✓  @{profile.username}  {total} 条帖子，准备下载最新 {limit} 条\n")
    done = skip = fail = 0
    i = 0

    while True:
        # 获取下一条帖子，遇到限流时自动等待重试
        post = None
        for attempt in range(1, RATE_LIMIT_RETRIES + 1):
            try:
                post = next(posts_iter)
                break
            except StopIteration:
                post = None
                break
            except KeyboardInterrupt:
                print("\n\n⚠️  手动中断")
                sys.exit(0)
            except Exception as e:
                if attempt < RATE_LIMIT_RETRIES:
                    wait = RATE_LIMIT_WAIT * attempt          # 5 / 10 / 15 / 20 分钟
                    jitter = random.uniform(0, 30)
                    total = int(wait + jitter)
                    print(f"\n⏳  被限流（第 {attempt} 次），等待 {total} 秒后重试…")
                    print(f"    已成功下载 {done} 条 | 错误: {e}")
                    try:
                        time.sleep(total)
                    except KeyboardInterrupt:
                        print("\n\n⚠️  手动中断")
                        sys.exit(0)
                else:
                    print(f"\n⚠️  获取帖子列表失败（已重试 {RATE_LIMIT_RETRIES} 次）: {e}")
                    print(f"    已成功下载 {done} 条，请等待更长时间后重新运行脚本")
                    print(f"    ※ 已下载的帖子不会重复下载，直接重跑即可续传")

        if post is None:
            break

        i += 1
        try:
            loc_str = f"  📍{post.location.name}" if post.location else ''
        except Exception:
            loc_str = ''
        print(f"[{i}/{limit}]  {post.date_local.strftime('%Y-%m-%d')}  "
              f"/p/{post.shortcode}{loc_str}")
        try:
            result = process_post(post, profile_dir, session, args.delay)
            if result == 'ok':
                done += 1
            elif result == 'skip':
                skip += 1
            else:
                fail += 1
        except KeyboardInterrupt:
            print("\n\n⚠️  手动中断")
            break
        except Exception as e:
            print(f"  ✗  意外错误: {e}")
            fail += 1

        if i < limit:
            jitter = random.uniform(0, args.delay * 0.5)   # ±50% 随机抖动，降低被识别概率
            time.sleep(args.delay + jitter)

    resolved = profile_dir.resolve()

    print(f"\n{'─'*48}")
    print(f"  ✅  完成  下载 {done} | 已有 {skip} | 失败 {fail}")
    print(f"{'─'*48}")
    print(f"\n  📂  文件夹已自动打开: {resolved}")
    print(f"  🔗  {resolved.as_uri()}")
    print(f"\n  📱  在本地相册中查看:")
    print(f"      1. 用 Chrome / Edge 打开 local-ig.html")
    print(f"      2. 点「选择文件夹」，选择上方目录")
    print()

    open_folder(resolved)


if __name__ == '__main__':
    main()
