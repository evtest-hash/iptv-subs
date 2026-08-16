#!/usr/bin/env python3
"""Derive a lite playlist from the full merged m3u: one best stream per channel.

Keeps the whole 央视 / 卫视 / 港澳台 groups, trims 地方 to one channel per
2-char city prefix, and preserves per-entry http-user-agent / http-referer
so players fetch 凤凰 etc. with the right UA.

Usage:
    python scripts/lite.py [--in tv/iptv.m3u] [--out tv/iptv_lite.m3u]
"""
import argparse
import os
import re

CAT_ORDER = {
    "央视": 0, "卫视": 1, "港澳台": 2, "地方": 3,
    "体育": 4, "电影": 5, "纪录": 6, "少儿": 7,
    "音乐": 8, "4K": 9, "特色": 10,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="infile", default="tv/iptv.m3u")
    ap.add_argument("--out", dest="outfile", default="tv/iptv_lite.m3u")
    args = ap.parse_args()

    lines = open(args.infile, encoding="utf-8").read().splitlines()
    header = lines[0] if lines and lines[0].startswith("#EXTM3U") else "#EXTM3U"

    # 解析 full m3u:每台 1..3 条 URL,取第一条(= 优先级最高, zbds 高清在前)
    entries = []
    i = 0
    while i < len(lines):
        l = lines[i]
        if l.startswith("#EXTINF"):
            attrs = l[len("#EXTINF"):]
            name = attrs.rsplit(",", 1)[-1].strip()
            g = re.search(r'group-title="([^"]*)"', attrs)
            tid = re.search(r'tvg-id="([^"]*)"', attrs)
            logo = re.search(r'tvg-logo="([^"]*)"', attrs)
            ua = re.search(r'http-user-agent="([^"]*)"', attrs)
            ref = re.search(r'http-referer="([^"]*)"', attrs)
            catch = re.search(r'catchup="([^"]*)"', attrs)
            csrc = re.search(r'catchup-source="([^"]*)"', attrs)
            url = None
            j = i + 1
            while j < len(lines) and lines[j].startswith("http"):
                if url is None:
                    url = lines[j]
                j += 1
            if url and name:
                entries.append({
                    "name": name,
                    "group": g.group(1) if g else "特色",
                    "id": tid.group(1) if tid else "",
                    "logo": logo.group(1) if logo else "",
                    "ua": ua.group(1) if ua else None,
                    "ref": ref.group(1) if ref else None,
                    "catchup": catch.group(1) if catch else None,
                    "catchup-source": csrc.group(1) if csrc else None,
                    "url": url,
                })
            i = j
        else:
            i += 1

    # 地方台按城市前缀(前 2 字)去重,保留每个城市第一个
    seen_city = set()
    kept = []
    for e in entries:
        if e["group"] == "地方":
            city = e["name"][:2]
            if city in seen_city:
                continue
            seen_city.add(city)
        kept.append(e)

    kept.sort(key=lambda e: (CAT_ORDER.get(e["group"], 99), e["name"]))

    out = [header]
    stats = {}
    for e in kept:
        attrs = ""
        if e["id"]:
            attrs += f' tvg-id="{e["id"]}"'
        if e["logo"]:
            attrs += f' tvg-logo="{e["logo"]}"'
        if e["ua"]:
            attrs += f' http-user-agent="{e["ua"]}"'
        if e["ref"]:
            attrs += f' http-referer="{e["ref"]}"'
        if e["catchup"]:
            attrs += f' catchup="{e["catchup"]}"'
            if e["catchup-source"]:
                attrs += f' catchup-source="{e["catchup-source"]}"'
        attrs += f' group-title="{e["group"]}",{e["name"]}'
        out.append("#EXTINF:-1" + attrs)
        out.append(e["url"])
        stats[e["group"]] = stats.get(e["group"], 0) + 1

    with open(args.outfile, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print(f"wrote {args.outfile}: {len(kept)} channels")
    for g in sorted(stats, key=lambda x: CAT_ORDER.get(x, 99)):
        print(f"  {g}: {stats[g]}")


if __name__ == "__main__":
    main()
