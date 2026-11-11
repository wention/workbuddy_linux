"""Render release notes, README block, changelog, badges and manifests."""

from __future__ import annotations

import re
from typing import Any

from .config import (
    API_URL_TEMPLATE,
    ARTICLE_LICENSE,
    ARTICLE_TITLE,
    ARTICLE_URL,
    CHANNELS,
)
from .models import Release
from .state import read_history

_BLOCK_RE_TEMPLATE = (
    r"<!-- AUTOGEN:{name}:START -->\n.*?\n<!-- AUTOGEN:{name}:END -->"
)

_KIND_LABELS = {
    "bootstrap": "首次收录",
    "upgrade": "版本升级",
    "downgrade": "版本回退",
    "rebuild": "同版本重新构建",
    "force": "手动强制重建",
}


def format_size(value: Any) -> str:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return "未知"
    if size <= 0:
        return "未知"
    return f"{size / 1024 / 1024:.1f} MiB"


def _records(state: dict[str, Any]) -> list[dict[str, Any]]:
    channels = state.get("channels") or {}
    return [channels[c.platform] for c in CHANNELS if c.platform in channels]


def _version_of(record: dict[str, Any]) -> str:
    return str(record.get("version") or "-")


def render_download_table(state: dict[str, Any]) -> str:
    records = _records(state)
    if not records:
        return "_等待首次自动检查后填充。_"
    lines = [
        "| 架构 | 包格式 | 适用发行版 | 版本 | 大小 | 下载 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for record in records:
        url = record.get("url") or ""
        lines.append(
            "| {arch} | `{pkg}` | {distro} | `{version}` | {size} | [下载 {pkg}]({url}) |".format(
                arch=record.get("arch", "-"),
                pkg=record.get("pkg", "-"),
                distro=record.get("distro", "-"),
                version=_version_of(record),
                size=format_size(record.get("size_bytes")),
                url=url,
            )
        )
    return "\n".join(lines)


def render_checksum_table(state: dict[str, Any]) -> str:
    records = _records(state)
    if not records:
        return "_暂无数据。_"
    lines = [
        "| 通道 | 实测 SHA256（下载后计算） | 接口 `sha256hash`（仅记录，不作校验） |",
        "| --- | --- | --- |",
    ]
    for record in records:
        real = f"`{record['verified_sha256']}`" if record.get("verified_sha256") else "未实测"
        upstream = (
            f"`{record['upstream_sha256']}`" if record.get("upstream_sha256") else "-"
        )
        lines.append(f"| `{record.get('platform')}` | {real} | {upstream} |")
    return "\n".join(lines)


def render_release_notes(
    release: Release,
    state: dict[str, Any],
    generated_at: str,
    *,
    verified: bool,
) -> str:
    channels = state.get("channels") or {}
    changed_lines = []
    for change in release.changes:
        label = _KIND_LABELS.get(change.kind, change.kind)
        previous = (
            f"（原 `{change.previous_version}`）"
            if change.previous_version
            and change.previous_version != change.snapshot.version
            else ""
        )
        changed_lines.append(
            f"- **{label}** `{change.channel.platform}` → `{change.snapshot.version}`{previous}"
        )

    deb = channels.get("workbuddy-linux-x64-deb") or {}
    rpm = channels.get("workbuddy-linux-x64-rpm") or {}
    deb_url = deb.get("url", API_URL_TEMPLATE.format(platform="workbuddy-linux-x64-deb"))
    rpm_url = rpm.get("url", API_URL_TEMPLATE.format(platform="workbuddy-linux-x64-rpm"))
    deb_name = deb.get("filename", "WorkBuddy-linux-x64-deb.deb")
    rpm_name = rpm.get("filename", "WorkBuddy-linux-x64-rpm.rpm")
    latest_api_url = API_URL_TEMPLATE.format(platform="workbuddy-linux-x64-deb")

    verify_note = (
        "本次发布已由 Actions 实际下载变更通道的安装包并计算 SHA256，见下表「实测」列。"
        if verified
        else "本次发布未开启下载校验，请安装前自行执行 `sha256sum` 校验文件。"
    )
    change_lines = "\n".join(changed_lines) if changed_lines else "- 无"

    return f"""# WorkBuddy Linux {release.tag}

> 由 GitHub Actions 自动检测并发布 · 检测时间：{generated_at}
> 本项目只是**官方更新接口的版本追踪器**，不是腾讯官方下载入口；安装包仍托管在腾讯 CDN 上。

## 本次变更

{change_lines}

## 四个通道

{render_download_table(state)}

## 校验和

{render_checksum_table(state)}

{verify_note}

> 上游 JSON 的 `sha256hash` 字段与文件实测值可能不一致（见[原文]({ARTICLE_URL})的实测记录），因此本表把它和实测值分开列出，**不要拿接口字段当校验标准**。

## 安装（本 Release 对应的固定版本）

Ubuntu / Debian / UOS / deepin 系，x86_64：

```bash
curl -fL -o {deb_name} "{deb_url}"
sha256sum {deb_name}
sudo apt install ./{deb_name}
```

Fedora / RHEL / openSUSE 系，x86_64：

```bash
curl -fL -o {rpm_name} "{rpm_url}"
sha256sum {rpm_name}
sudo dnf install ./{rpm_name}
```

ARM64 机器把命令里的 `x64` 换成 `arm64`，或用上表中对应架构的下载链接。
想始终安装最新版，可动态取地址（不要硬编码 URL）：

```bash
url=$(curl -fsSL "{latest_api_url}" | jq -r '.url')
curl -fL -O "$url"
```

## 说明

- 更新接口 `https://copilot.tencent.com/v2/update?platform=<channel>` 为**未公开接口**，随时可能变动；本项目每天自动检查四个通道。
- `supportsFastUpdate: false`，应用内更新在 Linux 上会指向官网，而官网没有 Linux 入口，因此升级请重新下载安装包。
- 依赖均为发行版标准库；deb 装到 `/opt/WorkBuddy/`，rpm 使用 Fedora 原生包名。
- 龙芯（LoongArch）没有官方包，deepin 商店里的版本为社区移植。
- 参考：[{ARTICLE_TITLE}]({ARTICLE_URL})（{ARTICLE_LICENSE} 署名-非商业性使用-相同方式共享）。
"""


def render_manifest(
    release: Release, state: dict[str, Any], generated_at: str
) -> dict[str, Any]:
    return {
        "schema": 1,
        "tag": release.tag,
        "version": release.version,
        "build": release.build,
        "change_kind": release.kind,
        "generated_at": generated_at,
        "article": ARTICLE_URL,
        "changed_channels": [c.snapshot.platform for c in release.changes],
        "channels": state.get("channels") or {},
    }


def render_latest_badge(state: dict[str, Any]) -> dict[str, Any]:
    version = state.get("latest_version") or "unknown"
    return {
        "schemaVersion": 1,
        "label": "WorkBuddy Linux",
        "message": f"v{version}",
        "color": "blue",
    }


def render_changelog(history: list[dict[str, Any]]) -> str:
    lines = [
        "# 更新日志",
        "",
        "> 本文件由 GitHub Actions 自动生成，请勿手工编辑。",
        "",
    ]
    if not history:
        lines.append("_尚无发布记录，等待首次自动检查。_")
        return "\n".join(lines) + "\n"
    for record in reversed(history):
        lines.append(f"## [{record.get('tag', '?')}] — {record.get('detected_at', '?')}")
        lines.append("")
        lines.append(f"- 变更类型：{_KIND_LABELS.get(record.get('kind', ''), record.get('kind', '?'))}")
        lines.append(f"- 通道数：{len(record.get('channels', []))}")
        for entry in record.get("channels", []):
            size = format_size(entry.get("size_bytes"))
            lines.append(
                f"- `{entry.get('platform')}` → `{entry.get('version')}`（{size}）"
                f" [下载]({entry.get('url', '')})"
            )
        lines.append("")
    return "\n".join(lines)


def update_block(text: str, name: str, block: str) -> str:
    """Replace content between AUTOGEN markers, or append them if absent."""

    pattern = re.compile(_BLOCK_RE_TEMPLATE.format(name=re.escape(name)), re.DOTALL)
    replacement = (
        f"<!-- AUTOGEN:{name}:START -->\n{block}\n<!-- AUTOGEN:{name}:END -->"
    )
    if pattern.search(text):
        return pattern.sub(lambda _: replacement, text, count=1)
    return text.rstrip() + "\n\n" + replacement + "\n"


def update_inline_block(text: str, name: str, block: str) -> str:
    """Replace content between ``/* AUTOGEN:<name>:START/END */`` JS markers."""

    start = f"/* AUTOGEN:{name}:START */"
    end = f"/* AUTOGEN:{name}:END */"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    replacement = f"{start} {block} {end}"
    if pattern.search(text):
        return pattern.sub(lambda _: replacement, text, count=1)
    return text


def render_summary(
    releases: list[Release],
    state: dict[str, Any],
    errors: dict[str, str],
    generated_at: str,
) -> str:
    lines = [
        "## WorkBuddy Linux 版本检查",
        "",
        f"- 检查时间：`{generated_at}`",
        f"- 当前上游版本：`{state.get('latest_version') or '未知'}`",
        f"- 本次发布：{len(releases)} 个 Release",
    ]
    for release in releases:
        lines.append(
            f"  - `{release.tag}` — {len(release.changes)} 个通道（{_KIND_LABELS.get(release.kind, release.kind)}）"
        )
    for platform, error in errors.items():
        lines.append(f"- ⚠️ `{platform}` 查询失败：`{error}`")
    lines.append("")
    return "\n".join(lines)
