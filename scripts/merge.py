#!/usr/bin/env python3
"""Build the full IPTV playlist from upstreams: dedup, categorize, validate, output.

Usage:
    python scripts/merge.py                    # HTTP liveness (default, fast)
    python scripts/merge.py --validate probe   # deep: download a segment & ffprobe real resolution
    python scripts/merge.py --validate speed   # measure throughput, fast-first order + write order.json
    python scripts/merge.py --no-validate      # structural only (CI / overseas runners)

Config: upstreams.json (upstreams / epg / http_proxy / max_candidates / min_resolution)
        config/alias.txt (台名归一) · blacklist.txt (剔除) · whitelist.txt (低清也保留的分类)
        config/order.txt (频道显示顺序, 未列出者自然序排尾; 卫视省台序在此维护)
        config/vod.txt (文件型点播 URL 子串, 命中移入 tv/vod.m3u; 电影循环流不列入)
Output: tv/iptv.m3u (直播播放器) + tv/iptv.txt (TVBox) + tv/vod.m3u (点播/归档, 与直播分离)
tvg-id: 不再透传上游源 id(zbds 内部数字 id 冲突且失稳), 统一按 EPG 命中 > 规范名分配,
        恒输出 tvg-name; EPG 抓取为加分项, best-effort, 失败静默回退规范名, 不影响直播。

Validation modes:
    none    — keep every URL (CI; 境外 Runner 对中国流探测会失真)
    http    — 跟重定向取前 4KB, 拒绝 HTML/JSON/空响应 (默认)
    probe   — 再下载一个 TS 分片 ffprobe 实测分辨率, 按分辨率给候选排序(高清在前)
              不做硬剔除: 免费源分段取流常因防热链/瞬时波动返回 4xx, 硬剔除会误杀
              (曾把可播的 CCTV5 判死)。死流剔除由 http 阶段负责。(本地低频跑)
    speed   — 实测每个候选的吞吐, 快源排前, 并写 config/order.json。
              CI(--no-validate)读取 order.json, 使境外 Runner 的产物保持本地测速顺序。
              建议在本地(国内网络)周期性跑一次刷新顺序。
"""
import argparse
import concurrent.futures
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

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


def load_order():
    """order.txt: 频道显示顺序(每行一个台名, 未列出者自然序排尾)。"""
    pos = {}
    for i, line in enumerate(read_lines(os.path.join(ROOT, "config", "order.txt"))):
        pos[norm(line)] = i
    return pos


def load_vod():
    """vod.txt: 文件型点播 URL 子串(每行一个, # 注释)。

    频道全部候选 URL 命中任一子串即视为点播, 从 live 列表移入 tv/vod.m3u。
    电影循环流(live.metshop.top 等)不列入, 保留在直播列表。"""
    return read_lines(os.path.join(ROOT, "config", "vod.txt"))


def fetch_epg_ids(epg_url, timeout=10):
    """抓 EPG 建 norm(显示名)->tvg-id 表。best-effort: 任何失败返回空 dict。

    51zmt 的 :8000 端口实为 http, 302 重定向到 https s.102031.xyz, urllib 跟随。
    https 直连失败时自动换 http 变体重试。仅作加分项: 失败静默回退规范名,
    绝不影响直播列表生成。"""
    if not epg_url:
        return {}
    tries = [epg_url]
    if epg_url.startswith("https://"):
        tries.append("http://" + epg_url[len("https://"):])
    for url in tries:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(800000)
            epg = {}
            for m in re.finditer(
                r'<channel[^>]*id="([^"]*)"[^>]*>\s*<display-name[^>]*>([^<]*)</display-name>',
                data.decode("utf-8", "ignore"),
            ):
                epg.setdefault(norm(m.group(2)), m.group(1))
            if epg:
                return epg
        except Exception:
            continue
    return {}


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


def clean_url(u):
    """清洗 URL: 去尾部空白/引号, 去 # 后重复完整 URL 片段, HTML 实体反复反解。

    保留 query 参数(?zzhed 等可能是防盗链必需)。"""
    u = u.strip().rstrip('"')
    if "#" in u:
        head, _, frag = u.partition("#")
        if "://" in frag:
            u = head
    for _ in range(3):
        nxt = html.unescape(u)
        if nxt == u:
            break
        u = nxt
    return u


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
        catch = re.search(r'catchup="([^"]*)"', attrs)
        csrc = re.search(r'catchup-source="([^"]*)"', attrs)
        url = next(
            (clean_url(u) for u in lines[i + 1:i + 4]
             if u.startswith("http")),
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
                "catchup": catch.group(1) if catch else None,
                "catchup-source": csrc.group(1) if csrc else None,
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


def natural_key(name):
    """自然排序键: 数字段按数值比较, 其余按字符。

    让 CCTV1 < CCTV2 < ... < CCTV10 < ... < CCTV17, 而非字符串序的
    CCTV1 < CCTV10 < CCTV2。拆出的数字/非数字段交替, 比较时同索引类型一致,
    不会触发 int vs str 的 TypeError。"""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name)]


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


def _download_speed(url, meta, proxy, window):
    """下载一个文件 window 秒, 返回 KB/s(失败=0)。"""
    fd, tmp = tempfile.mkstemp(dir=CACHE, suffix=".bin")
    os.close(fd)
    try:
        t0 = time.time()
        cmd = curl_base(proxy) + [
            "--max-time", str(window), "--max-filesize", "15000000",
            "-A", meta["ua"], "-o", tmp,
        ]
        if meta.get("ref"):
            cmd += ["-e", meta["ref"]]
        cmd.append(url)
        subprocess.run(cmd, capture_output=True, timeout=window + 5)
        dt = time.time() - t0
        sz = os.path.getsize(tmp)
        if dt <= 0 or sz <= 0:
            return 0.0
        return sz / 1024.0 / dt   # KB/s
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def measure_speed(url, meta, proxy, window=4):
    """实测吞吐: 取 HLS 的一个分片(或 FLV 直流)下载 window 秒, 返回 KB/s(失败=0)。"""
    head = _curl_bin(url, meta, proxy, range_="0-3")
    if head.startswith(b"FLV"):                       # 直连 FLV
        return _download_speed(url, meta, proxy, window)
    if not head.startswith(b"#"):
        return 0.0
    body = _curl_text(url, meta, proxy)
    if not body.startswith("#"):
        return 0.0
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
        return 0.0
    return _download_speed(urllib.parse.urljoin(url, seg), meta, proxy, window)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--validate", choices=["none", "http", "probe", "speed"], default="http",
                    help="none=跳过(CI), http=HTTP+内容校验(默认), probe=ffprobe实测分辨率排序, "
                         "speed=实测吞吐排序并写 config/order.json(CI 读它保序)")
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
    order_pos = load_order()
    vod_subs = load_vod()
    epg = cfg.get("epg", "")
    epg_ids = fetch_epg_ids(epg)   # 加分项: best-effort, 失败返回空, 不影响产物
    # 本地测速得到的每台候选排序(CI --no-validate 时读取以保持顺序不丢)
    order_path = os.path.join(ROOT, "config", "order.json")
    learned = {}
    if os.path.exists(order_path):
        with open(order_path, encoding="utf-8") as f:
            learned = json.load(f)
    print(f"upstreams: {[u['name'] for u in cfg['upstreams']]} | validate={mode}"
          f" | max_candidates={max_cands} | min_resolution={min_res}"
          f" | learned_order={len(learned)} channels | epg_ids={len(epg_ids)}"
          f" | vod_subs={len(vod_subs)}", flush=True)

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
            c = channels.setdefault(key, {"name": e["name"], "key": key, "urls": []})
            cand = classify(e)
            if c.get("rank", 99) > CAT_ORDER.get(cand, 99):
                c["rank"] = CAT_ORDER.get(cand, 99)
                c["cat"] = cand
            # 不再透传上游源 id(zbds 内部数字 id 既冲突又失稳), tvg-id 在输出阶段统一分配
            if not c.get("logo") and e["logo"]:
                c["logo"] = e["logo"]
            if not c.get("catchup") and e.get("catchup"):
                c["catchup"] = e["catchup"]
                c["catchup-source"] = e.get("catchup-source")
            if e["url"] not in [u for _, u in c["urls"]]:
                c["urls"].append((src, e["url"]))
                url_meta.setdefault(e["url"], {"ua": e["ua"] or DEFAULT_UA, "ref": e["ref"]})

    # VOD 判定: 全部候选 URL 命中 vod.txt 子串 → 点播, 从 live 移入 vod.m3u
    # (不能"任一命中即判": 东方卫视首候选是 kwimgs 点播文件但有直播兜底, 仍算直播)
    for c in channels.values():
        c["vod"] = bool(c["urls"]) and all(
            any(s in u for s in vod_subs) for _, u in c["urls"])

    all_urls = sorted({u for c in channels.values() for _, u in c["urls"]})
    url_cat = {u: c.get("cat", "") for c in channels.values() for _, u in c["urls"]}
    print(f"channels: {len(channels)} | candidate URLs: {len(all_urls)}", flush=True)

    # 3) 校验
    alive = set(all_urls)
    res_map = {}     # probe 模式填充: url -> 实测高度(0=未知)
    speed_map = {}   # speed 模式填充: url -> 实测吞吐 KB/s(0=未知)
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

        elif mode == "speed":
            print(f"validate[speed]: stage2 实测吞吐 ({len(alive)} URLs) ...", flush=True)

            def spd(u):
                return u, measure_speed(u, url_meta.get(u, {"ua": DEFAULT_UA, "ref": None}), proxy)

            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
                for i, (u, k) in enumerate(ex.map(spd, sorted(alive))):
                    speed_map[u] = k
                    if (i + 1) % 100 == 0:
                        fast = sum(1 for v in speed_map.values() if v >= 200)
                        print(f"  speed {i + 1}/{len(alive)}, >=200KB/s: {fast}", flush=True)
            # 写 config/order.json: 每台候选按实测吞吐降序, 供 CI(--no-validate)读取保持顺序
            learned = {}
            for key, c in channels.items():
                urls = sorted((u for s, u in c["urls"] if u in alive),
                              key=lambda u: -speed_map.get(u, 0.0))
                learned[key] = urls
            with open(order_path, "w", encoding="utf-8") as f:
                json.dump(learned, f, ensure_ascii=False, indent=0)
            vals = [v for v in speed_map.values() if v > 0]
            print(f"  speed 完成: 平均 {sum(vals) / max(len(vals), 1):.0f} KB/s"
                  f" (有吞吐 {len(vals)}/{len(speed_map)})", flush=True)

    # 4) 输出 m3u + txt (live) + vod.m3u (点播/归档)
    priority = {"zbds": 0, "guovin": 1, "aptv": 2}
    os.makedirs(os.path.join(ROOT, "tv"), exist_ok=True)
    m3u = [f'#EXTM3U x-tvg-url="{epg}"']
    txt = []          # TVBox 格式: 分类,#genre# + name,url1#url2
    vod = [f'#EXTM3U x-tvg-url="{epg}"', "# 点播/归档(非直播, 由 merge.py 从 live 列表分离)"]
    stats = {}
    kept = 0
    vod_n = 0
    used_tids = set()
    for cat in sorted({c["cat"] for c in channels.values()},
                      key=lambda x: CAT_ORDER.get(x, 99)):
        cat_live, cat_vod = [], []
        # 组内排序: order.txt 指定者按其顺序, 未指定者自然序排尾
        for c in sorted((c for c in channels.values() if c["cat"] == cat),
                        key=lambda c: (order_pos.get(c["key"], len(order_pos)),
                                       natural_key(c["name"]))):
            c["urls"].sort(key=lambda su: (priority.get(su[0], 9), su[1]))
            if res_map:   # probe 模式: 分辨率优先
                c["urls"].sort(key=lambda su: (-res_map.get(su[1], 0),
                                               priority.get(su[0], 9), su[1]))
            if speed_map: # speed 模式: 吞吐优先
                c["urls"].sort(key=lambda su: (-speed_map.get(su[1], 0.0),
                                               priority.get(su[0], 9), su[1]))
            # order.json(learned): 任何模式都应用, 保持本地测速得到的"快源在前"顺序
            learned_list = learned.get(c.get("key"), [])
            if learned_list:
                pos = {u: i for i, u in enumerate(learned_list)}
                c["urls"].sort(key=lambda su: (pos.get(su[1], 999), su[1]))
            picked = [u for s, u in c["urls"] if u in alive][:max_cands]
            if not picked:
                continue
            # tvg-id: EPG 命中 > 规范名, 全表唯一性兜底(加分项, 失败不影响直播)
            tid = epg_ids.get(c["key"]) or epg_ids.get(norm(c["name"])) or c["key"]
            base, n = tid, 2
            while tid in used_tids:
                tid = f"{base}-{n}"
                n += 1
            used_tids.add(tid)
            attrs = f' tvg-id="{tid}" tvg-name="{c["name"]}"'
            if c.get("logo"):
                attrs += f' tvg-logo="{c["logo"]}"'
            m = url_meta.get(picked[0], {})
            if m.get("ua") and m["ua"] != DEFAULT_UA:
                attrs += f' http-user-agent="{m["ua"]}"'
            if m.get("ref"):
                attrs += f' http-referer="{m["ref"]}"'
            if c.get("catchup"):
                attrs += f' catchup="{c["catchup"]}"'
                if c.get("catchup-source"):
                    attrs += f' catchup-source="{c["catchup-source"]}"'
            attrs += f' group-title="{cat}",{c["name"]}'
            if c.get("vod"):
                cat_vod.append((attrs, picked))
            else:
                cat_live.append((attrs, picked, c["name"]))
        if cat_live:
            txt.append(f"{cat},#genre#")
            for attrs, picked, name in cat_live:
                kept += 1
                stats[cat] = stats.get(cat, 0) + 1
                m3u.append("#EXTINF:-1" + attrs)
                m3u.extend(picked)
                txt.append(f'{name},{"#".join(picked)}')
        if cat_vod:
            vod.append(f"# === {cat} ===")
            for attrs, picked in cat_vod:
                vod_n += 1
                vod.append("#EXTINF:-1" + attrs)
                vod.extend(picked)

    m3u_path = os.path.join(ROOT, "tv", "iptv.m3u")
    txt_path = os.path.join(ROOT, "tv", "iptv.txt")
    vod_path = os.path.join(ROOT, "tv", "vod.m3u")
    with open(m3u_path, "w", encoding="utf-8") as f:
        f.write("\n".join(m3u) + "\n")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(txt) + "\n")
    with open(vod_path, "w", encoding="utf-8") as f:
        f.write("\n".join(vod) + "\n")
    print(f"wrote tv/iptv.m3u ({kept} live) + tv/iptv.txt + tv/vod.m3u ({vod_n} VOD)", flush=True)
    for g in sorted(stats, key=lambda x: CAT_ORDER.get(x, 99)):
        if stats[g]:
            print(f"  {g}: {stats[g]}")


if __name__ == "__main__":
    main()
