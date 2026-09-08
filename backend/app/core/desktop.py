"""
MASAÜSTÜ AJAN ARAÇLARI — algılama ve başlatma
==============================================

Kullanıcı, platformu kendi kullandığı ajan araçlarıyla (Claude Desktop,
Claude Code, Codex, Cursor, VS Code…) birlikte çalıştırmak ister. Bu modül:

  1. Aracın bu makinede kurulu olup olmadığını bulur (PATH + bilinen kurulum
     dizinleri; Windows'ta masaüstü uygulamaları PATH'e girmez),
  2. Kurulu değilse nasıl kurulacağını söyler,
  3. Kurulu ise kullanıcı isterse **başlatır**.

GÜVENLİK: Yalnızca bu dosyada ADI GEÇEN araçlar başlatılabilir. Serbest komut
çalıştırma yoktur; ne kullanıcı ne de ajan buraya rastgele bir yol geçiremez.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logging import get_logger

log = get_logger("zumvia.desktop")

WINDOWS = platform.system() == "Windows"
MACOS = platform.system() == "Darwin"


def _expand(*parts: str) -> Path | None:
    """Ortam değişkeni içeren yolu çözer; yoksa None."""
    try:
        path = Path(os.path.expandvars(os.path.join(*parts)))
    except (TypeError, ValueError):
        return None
    return path if "%" not in str(path) and "$" not in str(path) else None


@dataclass(frozen=True, slots=True)
class DesktopTool:
    id: str
    label: str
    kind: str                       # "cli" | "app"
    binaries: tuple[str, ...] = ()
    windows_paths: tuple[str, ...] = ()
    macos_paths: tuple[str, ...] = ()
    linux_paths: tuple[str, ...] = ()
    install: str = ""
    note: str = ""
    mcp_target: bool = False        # MCP sunucumuz buna bağlanabilir mi?
    extra: dict[str, Any] = field(default_factory=dict)


TOOLS: dict[str, DesktopTool] = {
    "claude_desktop": DesktopTool(
        id="claude_desktop", label="Claude Desktop", kind="app",
        windows_paths=(
            r"%LOCALAPPDATA%\AnthropicClaude\claude.exe",
            r"%LOCALAPPDATA%\Programs\Claude\Claude.exe",
            r"%PROGRAMFILES%\Claude\Claude.exe",
        ),
        macos_paths=("/Applications/Claude.app",),
        install="https://claude.ai/download",
        note="MCP bağlantısı için Ayarlar → Connectors bölümüne HTTP sunucumuzu ekleyin.",
        mcp_target=True,
    ),
    "claude_code": DesktopTool(
        id="claude_code", label="Claude Code", kind="cli",
        binaries=("claude",),
        windows_paths=(
            r"%APPDATA%\npm\claude.cmd",
            r"%LOCALAPPDATA%\Programs\claude\claude.exe",
            r"%USERPROFILE%\.local\bin\claude.exe",
            r"%USERPROFILE%\.claude\local\claude.exe",
        ),
        macos_paths=("/usr/local/bin/claude", "/opt/homebrew/bin/claude"),
        linux_paths=("/usr/local/bin/claude", "~/.local/bin/claude"),
        install="npm install -g @anthropic-ai/claude-code",
        note="Komuta ajanı olarak seçilebilir; MCP sunucumuza da bağlanır.",
        mcp_target=True,
    ),
    "codex": DesktopTool(
        id="codex", label="Codex CLI", kind="cli",
        binaries=("codex",),
        windows_paths=(r"%APPDATA%\npm\codex.cmd",),
        macos_paths=("/usr/local/bin/codex", "/opt/homebrew/bin/codex"),
        install="npm install -g @openai/codex",
        mcp_target=True,
    ),
    "gemini_cli": DesktopTool(
        id="gemini_cli", label="Gemini CLI", kind="cli",
        binaries=("gemini",),
        windows_paths=(r"%APPDATA%\npm\gemini.cmd",),
        macos_paths=("/usr/local/bin/gemini", "/opt/homebrew/bin/gemini"),
        install="npm install -g @google/gemini-cli",
        mcp_target=True,
    ),
    "opencode": DesktopTool(
        id="opencode", label="OpenCode", kind="cli",
        binaries=("opencode",),
        windows_paths=(r"%APPDATA%\npm\opencode.cmd",
                       r"%LOCALAPPDATA%\Programs\opencode\opencode.exe"),
        macos_paths=("/usr/local/bin/opencode", "/opt/homebrew/bin/opencode"),
        install="npm install -g opencode-ai",
        mcp_target=True,
    ),
    "cursor": DesktopTool(
        id="cursor", label="Cursor", kind="app",
        binaries=("cursor",),
        windows_paths=(r"%LOCALAPPDATA%\Programs\cursor\Cursor.exe",),
        macos_paths=("/Applications/Cursor.app",),
        install="https://cursor.com/download",
        mcp_target=True,
    ),
    "vscode": DesktopTool(
        id="vscode", label="VS Code", kind="app",
        binaries=("code",),
        windows_paths=(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
                       r"%PROGRAMFILES%\Microsoft VS Code\Code.exe"),
        macos_paths=("/Applications/Visual Studio Code.app",),
        install="https://code.visualstudio.com/download",
        mcp_target=True,
    ),
    "lmstudio": DesktopTool(
        id="lmstudio", label="LM Studio", kind="app",
        windows_paths=(r"%LOCALAPPDATA%\Programs\lm-studio\LM Studio.exe",),
        macos_paths=("/Applications/LM Studio.app",),
        install="https://lmstudio.ai",
        note="Yerel model sunucusu: sağlayıcı olarak 'LM Studio' seçin, ücretsizdir.",
    ),
    "ollama": DesktopTool(
        id="ollama", label="Ollama", kind="cli",
        binaries=("ollama",),
        windows_paths=(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe",),
        macos_paths=("/usr/local/bin/ollama", "/Applications/Ollama.app"),
        install="https://ollama.com/download",
        note="Yerel model sunucusu: internet ve API ücreti gerektirmez.",
    ),
}


def _locate(tool: DesktopTool) -> str:
    """Aracın çalıştırılabilir yolunu bulur; bulunamazsa boş dize."""
    for binary in tool.binaries:
        found = shutil.which(binary)
        if found:
            return found

    candidates = tool.windows_paths if WINDOWS else (
        tool.macos_paths if MACOS else tool.linux_paths)
    for raw in candidates:
        path = _expand(os.path.expanduser(raw))
        if path is not None and path.exists():
            return str(path)
    return ""


def detect_all() -> list[dict[str, Any]]:
    """Kurulu araçların listesi (arayüz ve ajan için)."""
    rows = []
    for tool in TOOLS.values():
        path = _locate(tool)
        rows.append({
            "id": tool.id, "label": tool.label, "kind": tool.kind,
            "available": bool(path), "path": path,
            "install": tool.install, "note": tool.note,
            "mcp_target": tool.mcp_target,
        })
    return rows


def launch(tool_id: str) -> dict[str, Any]:
    """
    Kurulu bir masaüstü aracını başlatır.

    Yalnızca yukarıdaki kayıt defterindeki araçlar açılabilir; kullanıcıdan ya
    da ajandan gelen serbest bir komut ASLA çalıştırılmaz.
    """
    tool = TOOLS.get(tool_id)
    if tool is None:
        return {"launched": False, "error": f"Bilinmeyen araç: {tool_id}"}

    path = _locate(tool)
    if not path:
        return {"launched": False, "error": f"{tool.label} bu makinede kurulu değil.",
                "install": tool.install}

    try:
        if MACOS and path.endswith(".app"):
            subprocess.Popen(["open", path])                       # noqa: S603,S607
        elif WINDOWS:
            os.startfile(path)                                     # noqa: S606
        else:
            subprocess.Popen([path], start_new_session=True)       # noqa: S603
    except OSError as exc:
        log.warning("araç başlatılamadı %s: %s", tool_id, exc)
        return {"launched": False, "error": f"Başlatılamadı: {exc}"}

    log.info("masaüstü aracı başlatıldı: %s", tool_id)
    return {"launched": True, "id": tool.id, "label": tool.label, "path": path}
