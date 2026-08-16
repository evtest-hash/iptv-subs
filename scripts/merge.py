#!/usr/bin/env python3
"""Build the full IPTV playlist from upstreams: dedup, categorize, validate, output.

Usage:
    python scripts/merge.py                    # HTTP liveness (default, fast)
    python scripts/merge.py --validate probe   # deep: download a segment & ffprobe real resolution
    python scripts/merge.py --no-validate      # structural only (CI / overseas runners)

Config: upstreams.json (upstreams / epg / http_proxy / max_candidates / min_resolution)
        config/alias.txt (台名归一) · blacklist.txt (剔除) · whitelist.txt (低清也保留的分类)
Output: tv/iptv.m3u (播放器) + tv/iptv.txt (TVBox 格式)

Validation modes:
    none    — keep every URL (CI; 境外 Runner 对中国流探测会失真)
    http    — 跟重定向取前 4KB, 拒绝 HTML/JSON/空响应 (默认)
    probe   — 再下载一个 TS 分片 ffprobe 实测分辨率, 按分辨率给候选排序(高清在前)
              不做硬剔除: 免费源分段取流常因防热链/瞬时波动返回 4xx, 硬剔除会误杀
              (曾把可播的 CCTV5 判死)。死流剔除由 http 阶段负责。(本地低频跑)
"""
import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "cache")
DEFAULT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

CAT_ORDER = {
    "央视": 0, "卫视": 1, "港澳台": 2, "地方": 3,
    "体育": 4, "电影": 5, "纪录": 6, "少儿": 7,
    "音乐": 8, "4K": 9, "特色": 10,
}
# 台名命中即归港澳台(优先级最高)
HK = re.compile(
    r"翡翠|明珠|TVB|TVBS|凤凰|东森|中天|华视|台视|民视|中视|三立|纬来|八大|"
    r"ViuTV|Viu|Now|澳门|港台|寰宇|年代|星卫|靖天|龙华|大爱|好消息|亚洲台|开电视|非凡|壹电视"
)
BADNAME = re.compile(r"^\d{4}-\d{2}-\d{2}|更新时间|^\s*$")


def load_config():
    with open(os.path.join(ROOT, "upstreams.json"), encoding="utf-8") as f:
        return json.load(f)


def read_lines(path):
    if not os.path.exists(path):
        return []
    return [l.strip() for l in open(path, encoding="utf-8") if l.strip() and not l.startswith("#")]


def load_alias():
    """alias.txt: 标准名|别名1|别名2  →  {norm(别名): norm(标准名)}"""
    mapping = {}
    for line in read_lines(os.path.join(ROOT, "config", "alias.txt")):
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) >= 2:
            canon = norm(parts[0])
            for a in parts[1:]:
                mapping[norm(a)] = canon
    return mapping


def load_blacklist():
    return read_lines(os.path.join(ROOT, "config", "blacklist.txt"))


def load_whitelist():
    return set(read_lines(os.path.join(ROOT, "config", "whitelist.txt")))


def curl_base(proxy):
    cmd = ["curl", "-sS", "-L"]
    if proxy:
        cmd += ["-x", proxy]
    return cmd


def fetch(name, url, ua, proxy, retries=3):
    """Download one upstream m3u into cache/. Returns path or None."""
    os.makedirs(CACHE, exist_ok=True)
    dest = os.path.join(CACHE, name + ".m3u")
    for attempt in range(1, retries + 1):
        r = subprocess.run(
            curl_base(proxy) + ["--max-time", "30", "-A", ua, "-o", dest, url],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and os.path.getsize(dest) > 100:
            return dest
        sys.stderr.write(f"  [warn] {name} download attempt {attempt} failed\n")
        time.sleep(2 * attempt)
    return None


def parse(m3u):
    """Parse an m3u file. Each entry: name/group/id/logo/ua/ref/url.

    http-user-agent / http-referer are carried per-entry: some sources
    (e.g. aptv 凤凰 = FLV) only respond to their declared UA.
    """
    entries = []
    lines = open(m3u, encoding="utf-8", errors="ignore").read().splitlines()
    for i, l in enumerate(lines):
        l = l.rstrip("\r")
        if not l.startswith("#EXTINF"):
            continue
        attrs = l[len("#EXTINF"):]
        name = attrs.rsplit(",", 1)[-1].strip()
        if not name:
            continue
        g = re.search(r'group-title="([^"]*)"', attrs)
        tid = re.search(r'tvg-id="([^"]*)"', attrs)
        logo = re.search(r'tvg-logo="([^"]*)"', attrs)
        ua = re.search(r'http-user-agent="([^"]*)"', attrs)
        ref = re.search(r'http-referer="([^"]*)"', attrs)
        url = next(
            (u.rstrip("\r") for u in lines[i + 1:i + 4]
             if u.rstrip("\r").startswith("http")),
            None,
        )
        if url:
            entries.append({
                "name": name,
                "group": g.group(1) if g else "",
                "id": tid.group(1) if tid else "",
                "logo": logo.group(1) if logo else "",
                "ua": ua.group(1) if ua else None,
                "ref": ref.group(1) if ref else None,
                "url": url,
            })
    return entries


def norm(name):
    """归一化台名用于跨源去重(去空格/连字符/常见后缀, 大写)。"""
    n = name.upper().replace(" ", "").replace("-", "")
    for suf in ("综合", "高清", "频道", "标清"):
        if n.endswith(suf):
            n = n[:-len(suf)]
    return n


def classify(e):
    """按台名/上游分组归属到统一分类。"""
    n, g = e["name"], e["group"]
    if HK.search(n):
        return "港澳台"
    if re.search(r"4K|8K|2160", n):
        return "4K"
    if re.search(r"CCTV|CGTN|CHC|央视", n):
        return "央视"
    if "卫视" in n:
        return "卫视"
    if "央视" in g or "付费" in g:
        return "央视"
    if "卫视" in g:
        return "卫视"
    if "电影" in g:
        return "电影"
    if "体育" in g:
        return "体育"
    if "纪录" in g or "纪实" in g:
        return "纪录"
    if "儿童" in g or "少儿" in g or "动画" in g:
        return "少儿"
    if "音乐" in g:
        return "音乐"
    if re.search(r"☘️|地方|频道$", g):
        return "地方"
    return "特色"


# ---------- validation ----------

def http_ok(url, meta, proxy):
    """Fast probe: fetch first bytes, reject HTML/JSON/empty, accept media.

    Must read as BINARY: aptv 凤凰等 FLV 流用 text=True 解码会抛 UnicodeDecodeError
    被误判为死台。这里字节级检查, 非 HTML/JSON/空即视为可播(HLS/FLV/TS)。"""
    cmd = curl_base(proxy) + ["--max-time", "6", "-A", meta["ua"], "-r", "0-4096"]
    if meta.get("ref"):
        cmd += ["-e", meta["ref"]]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=8)
        data = r.stdout
        if not data or not data.strip():
            return False
        low = data[:4096].lower()
        if b"<html" in low or b"<!doctype" in low or b"<head" in low:
            return False
        if low.lstrip().startswith((b"{", b"[")):
            return False
        return True
    except Exception:
        return False


def _curl_text(url, meta, proxy, max_time=12):
    cmd = curl_base(proxy) + ["--max-time", str(max_time), "-A", meta["ua"]]
    if meta.get("ref"):
        cmd += ["-e", meta["ref"]]
    cmd.append(url)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=max_time + 4).stdout
    except Exception:
        return ""


def _curl_bin(url, meta, proxy, max_time=8, range_=None):
    cmd = curl_base(proxy) + ["--max-time", str(max_time), "-A", meta["ua"]]
    if meta.get("ref"):
        cmd += ["-e", meta["ref"]]
    if range_:
        cmd += ["-r", range_]
    cmd.append(url)
    try:
        return subprocess.run(cmd, capture_output=True, timeout=max_time + 4).stdout
    except Exception:
        return b""


def _download_to_tmp(url, meta, proxy, max_time=15, max_bytes=8000000):
    """下载到临时文件, 返回 (tmp_path, http_code)。失败返回 (None, None)。"""
    fd, tmp = tempfile.mkstemp(dir=CACHE, suffix=".ts")
    os.close(fd)
    try:
        cmd = curl_base(proxy) + [
            "--max-time", str(max_time), "--max-filesize", str(max_bytes),
            "-A", meta["ua"], "-w", "%{http_code}", "-o", tmp,
        ]
        if meta.get("ref"):
            cmd += ["-e", meta["ref"]]
        cmd.append(url)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=max_time + 6)
        return tmp, r.stdout.strip()
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None, None


def _probe_height(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "stream=codec_type,width,height", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    h = 0
    for row in r.stdout.splitlines():
        parts = row.split(",")
        if parts and parts[0] == "video" and len(parts) >= 3 and parts[1].isdigit():
            h = max(h, int(parts[2]))
    return h


def probe_resolution(url, meta, proxy):
    """Deep probe: real resolution by downloading a segment/stream + ffprobe.

    Returns (height, status):
      ('ok', h>0)      — 实测到视频分辨率
      ('error', 0)     — 分段/流返回明确 4xx/5xx 或非媒体 → 判死
      ('unknown', 0)   — 拿不到清晰结论(防刷/token/偶发) → 保留
    Handles HLS (master/media) and direct FLV streams (aptv 凤凰).
    """
    head = _curl_bin(url, meta, proxy, range_="0-3")
    if head.startswith(b"FLV"):                       # 直连 FLV 流
        tmp, code = _download_to_tmp(url, meta, proxy)
        if not tmp:
            return 0, "error"
        h = _probe_height(tmp)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return (h, "ok") if h > 0 else (0, "unknown")
    if not head.startswith(b"#"):
        return 0, "error"
    body = _curl_text(url, meta, proxy)
    if not body.startswith("#"):
        return 0, "error"
    lines = body.splitlines()
    seg = None
    if "#EXT-X-STREAM-INF" in body:                   # master → 第一个变体
        for i, l in enumerate(lines):
            if l.startswith("#EXT-X-STREAM-INF"):
                seg = lines[i + 1]
                break
    else:                                             # media → 第一个分片
        for l in lines:
            if l and not l.startswith("#"):
                seg = l
                break
    if not seg:
        return 0, "unknown"
    seg_url = urllib.parse.urljoin(url, seg)
    tmp, code = _download_to_tmp(seg_url, meta, proxy)
    if not tmp:
        return 0, "error"
    h = _probe_height(tmp)
    try:
        os.remove(tmp)
    except OSError:
        pass
    if code and code.startswith(("4", "5")):
        return 0, "error"
    return (h, "ok") if h > 0 else (0, "unknown")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--validate", choices=["none", "http", "probe"], default="http",
                    help="none=跳过(CI), http=HTTP+内容校验(默认), probe=ffprobe实测分辨率并排序候选(不硬剔除)")
    ap.add_argument("--no-validate", action="store_true", help="等价于 --validate none")
    args = ap.parse_args()
    mode = "none" if args.no_validate else args.validate

    cfg = load_config()
    proxy = cfg.get("http_proxy", "") or ""
    max_cands = int(cfg.get("max_candidates", 5))
    min_res = int(cfg.get("min_resolution", 480))
    alias_map = load_alias()
    blacklist = load_blacklist()
    whitelist = load_whitelist()
    epg = cfg.get("epg", "")
    print(f"upstreams: {[u['name'] for u in cfg['upstreams']]} | validate={mode}"
          f" | max_candidates={max_cands} | min_resolution={min_res}", flush=True)

    # 1) 下载上游
    files = {}
    for u in cfg["upstreams"]:
        f = fetch(u["name"], u["url"], u.get("ua", DEFAULT_UA), proxy)
        if f:
            files[u["name"]] = f
            print(f"  [ok] {u['name']}: {sum(1 for _ in open(f))} lines", flush=True)
        else:
            sys.stderr.write(f"  [warn] {u['name']} failed; skipped\n")
    if not files:
        sys.exit(2)

    # 2) 解析 / 别名 / 黑白名单 / 去重 / 分类
    url_meta = {}   # url -> {ua, ref}
    channels = {}   # norm key -> {name, cat, id, logo, urls: [(src, url)]}
    for src, f in files.items():
        for e in parse(f):
            if BADNAME.search(e["name"]):
                continue
            key = norm(e["name"])
            key = alias_map.get(key, key)          # 别名归一
            if not key:
                continue
            if blacklist and any(b in key for b in blacklist):
                continue
            c = channels.setdefault(key, {"name": e["name"], "urls": []})
            cand = classify(e)
            if c.get("rank", 99) > CAT_ORDER.get(cand, 99):
                c["rank"] = CAT_ORDER.get(cand, 99)
                c["cat"] = cand
            if not c.get("id") and e["id"]:
                c["id"] = e["id"]
            if not c.get("logo") and e["logo"]:
                c["logo"] = e["logo"]
            if e["url"] not in [u for _, u in c["urls"]]:
                c["urls"].append((src, e["url"]))
                url_meta.setdefault(e["url"], {"ua": e["ua"] or DEFAULT_UA, "ref": e["ref"]})

    all_urls = sorted({u for c in channels.values() for _, u in c["urls"]})
    url_cat = {u: c.get("cat", "") for c in channels.values() for _, u in c["urls"]}
    print(f"channels: {len(channels)} | candidate URLs: {len(all_urls)}", flush=True)

    # 3) 校验
    alive = set(all_urls)
    res_map = {}   # probe 模式填充: url -> 实测高度(0=未知)
    if mode == "none":
        print("validate: skipped", flush=True)
    else:
        def check_http(url):
            meta = url_meta.get(url, {"ua": DEFAULT_UA, "ref": None})
            for _ in range(2):                  # 慢中转给第二次机会
                if http_ok(url, meta, proxy):
                    return True
            return False

        print(f"validate[{mode}]: stage1 HTTP ...", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
            for i, (u, ok) in enumerate(zip(all_urls, ex.map(check_http, all_urls))):
                if not ok:
                    alive.discard(u)
                if (i + 1) % 200 == 0:
                    print(f"  http {i + 1}/{len(all_urls)}, alive {len(alive)}", flush=True)
        print(f"  http alive: {len(alive)}/{len(all_urls)}", flush=True)

        if mode == "probe":
            if not shutil.which("ffprobe"):
                print("  [warn] ffprobe 未安装, 降级为 http 校验", flush=True)
            else:
                print(f"validate[probe]: stage2 ffprobe (rank + 诊断, 不硬剔除) ...", flush=True)
                res_map = {}
                status_map = {}

                def probe(u):
                    h, st = probe_resolution(u, url_meta.get(u, {"ua": DEFAULT_UA, "ref": None}), proxy)
                    return u, h, st

                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                    for i, (u, h, st) in enumerate(ex.map(probe, sorted(alive))):
                        res_map[u] = h
                        status_map[u] = st
                        if (i + 1) % 100 == 0:
                            ok = sum(1 for v in status_map.values() if v == "ok")
                            err = sum(1 for v in status_map.values() if v == "error")
                            print(f"  probe {i + 1}, ok {ok} / err {err}", flush=True)
                # 不做硬剔除: 免费源分段取流常因防热链/瞬时波动返回 4xx,
                # 单次探测误杀过 CCTV5(单独测是 1080p ok)。probe 只负责:
                #   ① 按实测分辨率给候选排序(高清在前) ② 打印诊断信息。
                # 死流剔除仍由 http 阶段负责。
                ok = sum(1 for v in status_map.values() if v == "ok")
                err = sum(1 for v in status_map.values() if v == "error")
                print(f"  probe 诊断: 实测到分辨率 {ok}/{len(alive)}, 疑似不可播 {err}"
                      f"(保留, 可自行 review)", flush=True)
                ok = sum(1 for u in alive)
                print(f"  probe alive (video, res>=min or whitelist): {ok}/{len(all_urls)}", flush=True)

    # 4) 输出 m3u + txt
    priority = {"zbds": 0, "guovin": 1, "aptv": 2}
    os.makedirs(os.path.join(ROOT, "tv"), exist_ok=True)
    m3u = [f'#EXTM3U x-tvg-url="{epg}"']
    txt = []          # TVBox 格式: 分类,#genre# + name,url1#url2
    stats = {}
    kept = 0
    for cat in sorted({c["cat"] for c in channels.values()},
                      key=lambda x: CAT_ORDER.get(x, 99)):
        stats[cat] = 0
        txt.append(f"{cat},#genre#")
        for c in sorted((c for c in channels.values() if c["cat"] == cat),
                        key=lambda c: c["name"]):
            c["urls"].sort(key=lambda su: (priority.get(su[0], 9), su[1]))
            if res_map:   # probe 模式: 实测分辨率高的候选排前面
                c["urls"].sort(key=lambda su: (-res_map.get(su[1], 0),
                                               priority.get(su[0], 9), su[1]))
            picked = [u for s, u in c["urls"] if u in alive][:max_cands]
            if not picked:
                continue
            kept += 1
            stats[cat] += 1
            attrs = ""
            if c.get("id"):
                attrs += f' tvg-id="{c["id"]}"'
            if c.get("logo"):
                attrs += f' tvg-logo="{c["logo"]}"'
            m = url_meta.get(picked[0], {})
            if m.get("ua") and m["ua"] != DEFAULT_UA:
                attrs += f' http-user-agent="{m["ua"]}"'
            if m.get("ref"):
                attrs += f' http-referer="{m["ref"]}"'
            attrs += f' group-title="{cat}",{c["name"]}'
            m3u.append("#EXTINF:-1" + attrs)
            m3u.extend(picked)
            txt.append(f'{c["name"]},{"#".join(picked)}')

    m3u_path = os.path.join(ROOT, "tv", "iptv.m3u")
    txt_path = os.path.join(ROOT, "tv", "iptv.txt")
    with open(m3u_path, "w", encoding="utf-8") as f:
        f.write("\n".join(m3u) + "\n")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(txt) + "\n")
    print(f"wrote tv/iptv.m3u ({kept} channels) + tv/iptv.txt", flush=True)
    for g in sorted(stats, key=lambda x: CAT_ORDER.get(x, 99)):
        if stats[g]:
            print(f"  {g}: {stats[g]}")


if __name__ == "__main__":
    main()
