#!/usr/bin/env python3
"""
flux — stream any site upscaled to 4K via mpv + VAAPI.
Supports YouTube, HiAnime, Crunchyroll, AsiaFlix (via m3u8) and 1000+ yt-dlp sites.
Arch Linux / AMD GPU.

stdlib only. Python ≥ 3.10.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NamedTuple

_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def log(msg: str) -> None:
    print(_c("1;32", "[✔]") + f" {msg}")


def warn(msg: str) -> None:
    print(_c("1;33", "[!]") + f" {msg}")


def err(msg: str) -> None:
    print(_c("1;31", "[✘]") + f" {msg}", file=sys.stderr)


def hdr(msg: str) -> None:
    print(_c("1;36", f"\n━━  {msg}  ━━"))


@dataclass(frozen=True)
class Config:
    url: str
    shader: str = "auto"
    gpu_api: str = "vulkan"
    download: bool = False
    quality: str = "bestvideo+bestaudio/best"
    cookies: str | None = None
    episodes: str | None = None
    autonext: bool = False
    skip_intro: bool = False
    target_w: int = 3840
    target_h: int = 2160
    cache_dir: Path = Path.home() / ".cache" / "flux"


def normalize_url(raw: str) -> str:
    """Decode fully percent-encoded URLs (e.g. from Stream Detector) and strip
    the &headers={} suffix that some browser extensions append.
    Only decodes when the whole URL is encoded — leaves normal URLs with
    encoded query params untouched so proxy URLs stay valid."""
    url = raw.strip()
    if url.startswith("%"):
        url = urllib.parse.unquote(url)
    # Strip Stream Detector's &headers=... suffix
    url = re.sub(r"&headers=.*$", "", url)
    return url


_ANIME_SITES = re.compile(
    r"hianime|aniwatch|crunchyroll|funimation|animepahe|9anime|zoro\.to|gogoanime", re.I
)
_HIANIME_SITES = re.compile(r"hianime|aniwatch", re.I)
_ASIAFLIX_HOSTS = re.compile(
    r"asiaflix\.(net|in|org|app)|dramacool\.lat|kisskh\.cyou", re.I
)
_DIRECT_MEDIA = re.compile(
    r"\.(m3u8|mp4|ts|mkv|webm)(\?|$)|/m3u8-proxy\?|/hls\d*/", re.I
)


class UrlKind:
    HIANIME = "hianime"
    ASIAFLIX_PAGE = "asiaflix_page"
    DIRECT_MEDIA = "direct_media"
    GENERIC = "generic"


def classify_url(url: str) -> str:
    if _HIANIME_SITES.search(url):
        return UrlKind.HIANIME
    if _ASIAFLIX_HOSTS.search(url) and not _DIRECT_MEDIA.search(url):
        return UrlKind.ASIAFLIX_PAGE
    if _DIRECT_MEDIA.search(url):
        return UrlKind.DIRECT_MEDIA
    return UrlKind.GENERIC


def check_deps(deps: list[str]) -> None:
    hdr("Checking dependencies")
    missing = []
    for dep in deps:
        path = shutil.which(dep)
        if path:
            log(f"{dep} found at {path}")
        else:
            err(f"{dep} is not installed.  sudo pacman -S {dep}")
            missing.append(dep)
    if missing:
        sys.exit(1)


class VaapiInfo(NamedTuple):
    device: str | None  # e.g. /dev/dri/renderD128
    hwdec: str  # "vaapi" or "no"


def detect_vaapi() -> VaapiInfo:
    hdr("Detecting AMD GPU")

    # Detect AMD GPU via lspci
    try:
        lspci = subprocess.run(["lspci"], capture_output=True, text=True)
        if not re.search(r"AMD|ATI|radeon|amdgpu", lspci.stdout, re.I):
            warn("No AMD GPU detected via lspci — hardware decode may not work.")
    except FileNotFoundError:
        warn("lspci not found — skipping GPU detection.")

    # Find DRI render node
    candidates = [Path(f"/dev/dri/renderD{n}") for n in (128, 129, 130)]
    device = next((str(p) for p in candidates if p.is_char_device()), None)

    if device is None:
        warn("No /dev/dri/renderD* node found — falling back to software decode.")
        warn("Make sure amdgpu kernel module is loaded:  lsmod | grep amdgpu")
        return VaapiInfo(device=None, hwdec="no")

    log(f"Using DRI render node: {device}")

    # Optional vainfo validation
    if shutil.which("vainfo"):
        result = subprocess.run(
            ["vainfo", "--display", "drm", "--device", device],
            capture_output=True,
        )
        if result.returncode == 0:
            log(f"VAAPI validated OK on {device}")
        else:
            warn(f"vainfo check failed on {device}")
            warn(
                "Install libva-mesa-driver if missing:  sudo pacman -S libva-mesa-driver"
            )
    else:
        warn(
            "vainfo not found (install libva-utils to validate VAAPI) — continuing anyway"
        )

    return VaapiInfo(device=device, hwdec="vaapi")


def ensure_hianime_plugin() -> None:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    plugin_dir = data_home / "yt-dlp" / "plugins" / "hianime"

    if plugin_dir.is_dir():
        log("yt-dlp-hianime plugin found")
        return

    hdr("Installing yt-dlp HiAnime plugin")
    pip = shutil.which("pip3") or shutil.which("pip")
    if pip is None:
        err("pip not found.  sudo pacman -S python-pip")
        sys.exit(1)

    log("Installing yt-dlp-hianime via pip…")
    result = subprocess.run(
        [
            pip,
            "install",
            "-U",
            "--quiet",
            "--break-system-packages",
            "https://github.com/pratikpatel8982/yt-dlp-hianime/archive/master.zip",
        ]
    )
    if result.returncode != 0:
        err("Failed to install yt-dlp-hianime plugin")
        sys.exit(1)
    log("yt-dlp-hianime plugin installed")


def resolve_autonext(url: str, episodes: str | None) -> tuple[str, str | None]:
    """For HiAnime ?ep=XXXXX URLs, fetch the show playlist and return
    (show_url, 'N-') so playback starts at the current episode and continues."""

    if episodes is not None:
        warn(
            f"Auto-next: -e range already set ({episodes}) — skipping playlist resolution"
        )
        return url, episodes

    ep_match = re.search(r"[?&]ep=(\d+)", url)
    if not ep_match:
        warn("Auto-next: could not find ep= in URL — playing single episode")
        return url, None

    ep_id = ep_match.group(1)

    # Convert /watch/show-slug?ep=N  →  /show-slug  (the show's playlist page)
    show_url = re.sub(r"/watch/", "/", url)
    show_url = re.sub(r"\?ep=.*", "", show_url)

    hdr("Resolving episode playlist")
    log(f"Fetching playlist from: {show_url}")

    result = subprocess.run(
        ["yt-dlp", "--flat-playlist", "--print", "id", show_url],
        capture_output=True,
        text=True,
    )
    ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]

    if not ids:
        warn("Auto-next: playlist fetch failed — playing single episode only")
        return url, None

    try:
        idx = ids.index(ep_id) + 1  # 1-based
    except ValueError:
        warn(
            f"Auto-next: ep {ep_id} not found in playlist — playing single episode only"
        )
        return url, None

    log(f"Starting at episode {idx} of {len(ids)}")
    return show_url, f"{idx}-"


def resolve_shader(requested: str, url: str) -> str:
    if requested != "auto":
        return requested
    if _ANIME_SITES.search(url):
        log("Auto-detected anime site — using anime4k shader")
        return "anime4k"
    if _ASIAFLIX_HOSTS.search(url):
        log("Auto-detected AsiaFlix (live-action drama) — using ravu shader")
        return "ravu"
    log("Auto-detected live-action source — using ravu shader")
    return "ravu"


def _fetch(url: str, dest: Path) -> None:
    log(f"Downloading: {dest.name}")
    with urllib.request.urlopen(url) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def _ensure_shader(name: str, url: str, shader_dir: Path) -> Path:
    dest = shader_dir / name
    if dest.exists():
        log(f"Shader cached: {dest}")
    else:
        _fetch(url, dest)
        log(f"Saved to {dest}")
    return dest


def _ensure_anime4k(shader_dir: Path, cache_dir: Path) -> Path:
    sentinel = shader_dir / "Anime4K_Clamp_Highlights.glsl"
    if sentinel.exists():
        log("Anime4K shader pack cached")
        return shader_dir

    zip_path = cache_dir / "anime4k-highend.zip"
    if not zip_path.exists():
        log("Downloading Anime4K v4 shader pack…")
        _fetch(
            "https://github.com/bloc97/Anime4K/releases/download/v4.0.1/Anime4K_v4.0.zip",
            zip_path,
        )

    log("Extracting shaders…")
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            if member.endswith(".glsl"):
                dest = shader_dir / Path(member).name
                dest.write_bytes(zf.read(member))
    log(f"Shaders extracted to {shader_dir}")

    if not sentinel.exists():
        err(f"Shader files not found after extraction in {shader_dir}")
        err(f"Try: rm -rf {cache_dir} and re-run")
        sys.exit(1)

    return shader_dir


_RAVU_URL = (
    "https://raw.githubusercontent.com/bjin/mpv-prescalers/master/"
    "gather/ravu-zoom-r3.hook"
)
_FSR_URL = (
    "https://gist.githubusercontent.com/agyild/82219c545228d70c5604f865ce0b0ce5"
    "/raw/2623d743b9c23f500ba086f05b385dcb1557e15d/FSR.glsl"
)


def build_shader_opts(shader: str, cache_dir: Path) -> list[str]:
    shader_dir = cache_dir / "shaders"
    shader_dir.mkdir(parents=True, exist_ok=True)

    match shader:
        case "ravu":
            sh = _ensure_shader("ravu-zoom-r3.hook", _RAVU_URL, shader_dir)
            log("RAVU-Zoom-R3 (gather) shader loaded")
            return [
                f"--glsl-shaders={sh}",
                "--scale=ewa_lanczos",
                "--cscale=ewa_lanczos",
                "--dscale=mitchell",
                "--linear-downscaling=no",
            ]
        case "anime4k":
            _ensure_anime4k(shader_dir, cache_dir)
            d = shader_dir
            shader_list = ":".join(
                str(d / n)
                for n in [
                    "Anime4K_Clamp_Highlights.glsl",
                    "Anime4K_Restore_CNN_VL.glsl",
                    "Anime4K_Upscale_CNN_x2_VL.glsl",
                    "Anime4K_AutoDownscalePre_x2.glsl",
                    "Anime4K_AutoDownscalePre_x4.glsl",
                    "Anime4K_Upscale_CNN_x2_M.glsl",
                ]
            )
            log("Anime4K v4 Mode A (HQ) shaders loaded")
            return [f"--glsl-shaders={shader_list}", "--scale=lanczos"]
        case "fsr":
            sh = _ensure_shader("FSR.glsl", _FSR_URL, shader_dir)
            log("AMD FSR shader loaded")
            return [f"--glsl-shaders={sh}", "--scale=bilinear"]
        case "hq":
            log("Using mpv built-in ewa_lanczossharp")
            return [
                "--scale=ewa_lanczossharp",
                "--cscale=ewa_lanczossharp",
                "--dscale=mitchell",
                "--linear-downscaling=no",
                "--sigmoid-upscaling=yes",
            ]
        case "none":
            warn("No upscaling shader — bilinear only")
            return ["--scale=bilinear"]
        case _:
            err(f"Unknown shader '{shader}'. Choose: ravu | anime4k | fsr | hq | none")
            sys.exit(1)


def build_mpv_args(
    cfg: Config,
    shader_opts: list[str],
    vaapi: VaapiInfo,
) -> list[str]:
    hdr("Building mpv configuration")

    args = [
        f"--gpu-api={cfg.gpu_api}",
        f"--hwdec={vaapi.hwdec}",
        f"--geometry={cfg.target_w}x{cfg.target_h}",
        f"--autofit={cfg.target_w}x{cfg.target_h}",
        f"--osd-msg1=Upscaling: {cfg.shader} | 4K {cfg.target_w}x{cfg.target_h}",
        *shader_opts,
    ]

    if vaapi.device:
        args.append(f"--vaapi-device={vaapi.device}")

    if cfg.skip_intro:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        script = config_home / "mpv" / "scripts" / "skip_intro.lua"
        if script.exists():
            args.append(f"--script={script}")
            log("Skip intro: enabled")
        else:
            warn(f"skip_intro.lua not found at {script}")
            warn("Re-run install.sh to install it, or copy skip_intro.lua manually")

    return args


def _run(cmd: list[str]) -> None:
    """Run a command, replacing the current process (no return)."""
    os.execvp(cmd[0], cmd)


def play_download(cfg: Config, mpv_args: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="flux-") as tmpdir:
        log(f"Downloading to {tmpdir} …")

        ytdlp_args = [
            "yt-dlp",
            "--format",
            cfg.quality,
            "--merge-output-format",
            "mkv",
            "--output",
            f"{tmpdir}/%(playlist_index)s-%(title)s.%(ext)s",
        ]
        if cfg.cookies:
            ytdlp_args += ["--cookies", cfg.cookies]
        if cfg.episodes:
            ytdlp_args += ["--playlist-items", cfg.episodes]
        ytdlp_args.append(cfg.url)

        result = subprocess.run(ytdlp_args)
        if result.returncode != 0:
            err("yt-dlp download failed")
            sys.exit(result.returncode)

        files = sorted(Path(tmpdir).iterdir())
        if not files:
            err("Download failed — no file found.")
            sys.exit(1)

        log(f"Download complete: {files[0]}")
        log("Starting mpv with 4K upscaling…")
        subprocess.run(["mpv", *mpv_args, str(files[0])])


def play_direct(cfg: Config, mpv_args: list[str]) -> None:
    """Direct .m3u8 / CDN URL — bypass yt-dlp entirely."""
    log("Streaming direct media URL via mpv…")
    _run(
        [
            "mpv",
            *mpv_args,
            "--no-ytdl",
            "--referrer=https://asiaflix.in/",
            "--force-media-title=Stream",
            "--sub-auto=fuzzy",
            "--slang=en,eng,und,zxx",
            "--subs-with-matching-audio=yes",
            "--sub-forced-events-only=no",
            cfg.url,
        ]
    )


def play_stream(cfg: Config, mpv_args: list[str]) -> None:
    """Stream via mpv's yt-dlp integration."""
    log("Streaming via yt-dlp integration…")

    raw_opts = []
    if cfg.cookies:
        raw_opts.append(f"--ytdl-raw-options-append=cookies={cfg.cookies}")
    if cfg.episodes:
        raw_opts.append(f"--ytdl-raw-options-append=playlist-items={cfg.episodes}")

    _run(
        [
            "mpv",
            *mpv_args,
            f"--ytdl-format={cfg.quality}",
            *raw_opts,
            cfg.url,
        ]
    )


def parse_args() -> Config:
    p = argparse.ArgumentParser(
        prog="flux",
        description="Stream any site upscaled to 4K via mpv + VAAPI.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
examples:
  flux 'https://youtu.be/dQw4w9WgXcQ'
  flux 'https://hianime.to/watch/one-piece-100?ep=1234'
  flux -n -i 'https://hianime.to/watch/show-title-123?ep=456'
  flux -s anime4k -d -e 1-3 'https://hianime.to/one-piece-100'
  flux -c ~/.config/cookies.txt 'https://crunchyroll.com/watch/abc'
  flux 'https://hlsproxy2.asiaflix.net/m3u8-proxy?url=...'

note (zsh):  always quote URLs containing '?' to prevent glob expansion.
        """,
    )
    p.add_argument("url", help="URL to stream or download")
    p.add_argument(
        "-s",
        dest="shader",
        default="auto",
        choices=["auto", "ravu", "anime4k", "fsr", "hq", "none"],
        help="upscaling shader (default: auto)",
    )
    p.add_argument(
        "-n",
        dest="autonext",
        action="store_true",
        help="auto-play next episodes (HiAnime: continues from current ep)",
    )
    p.add_argument(
        "-i",
        dest="skip_intro",
        action="store_true",
        help="skip intro/OP automatically via chapter markers",
    )
    p.add_argument(
        "-d",
        dest="download",
        action="store_true",
        help="download before playing (avoids buffering)",
    )
    p.add_argument(
        "-q",
        dest="quality",
        default="bestvideo+bestaudio/best",
        metavar="FMT",
        help="yt-dlp format selector",
    )
    p.add_argument(
        "-e",
        dest="episodes",
        default=None,
        metavar="RANGE",
        help="playlist range: '1'  '1-3'  '2,4,6'",
    )
    p.add_argument(
        "-c",
        dest="cookies",
        default=None,
        metavar="FILE",
        help="cookies.txt for login-gated sites (Netscape format)",
    )
    p.add_argument(
        "-g",
        dest="gpu_api",
        default="vulkan",
        choices=["vulkan", "opengl"],
        help="GPU API (default: vulkan)",
    )

    a = p.parse_args()

    cache_dir = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "flux"

    return Config(
        url=normalize_url(a.url),
        shader=a.shader,
        gpu_api=a.gpu_api,
        download=a.download,
        quality=a.quality,
        cookies=a.cookies,
        episodes=a.episodes,
        autonext=a.autonext,
        skip_intro=a.skip_intro,
        cache_dir=cache_dir,
    )


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    cfg = parse_args()

    check_deps(["mpv", "yt-dlp"])

    vaapi = detect_vaapi()

    kind = classify_url(cfg.url)

    # Site-specific setup
    if kind == UrlKind.HIANIME:
        ensure_hianime_plugin()
        if not cfg.autonext:
            new_url, new_episodes = resolve_autonext(cfg.url, cfg.episodes)
            cfg = replace(cfg, url=new_url, episodes=new_episodes)

    # Resolve shader
    shader = resolve_shader(cfg.shader, cfg.url)
    cfg = replace(cfg, shader=shader)

    hdr(f"Setting up shaders  [{shader}]")
    shader_opts = build_shader_opts(shader, cfg.cache_dir)

    mpv_args = build_mpv_args(cfg, shader_opts, vaapi)

    hdr("Launching playback")

    if kind == UrlKind.DIRECT_MEDIA:
        play_direct(cfg, mpv_args)
    elif cfg.download:
        play_download(cfg, mpv_args)
    else:
        play_stream(cfg, mpv_args)


if __name__ == "__main__":
    main()
