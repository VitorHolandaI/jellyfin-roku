"""Servico de quadros para o modo Explorar do app Roku (protótipo).

Uso: python3 frame_server.py --jellyfin http://127.0.0.1:8096 \
         --map /media=/home/vitor/jellyfin-test/media [--port 8790]
GET /frame?item=<id>&t=<seg>&x=&y=&w=&h=&out=<px>&api_key=<token do Jellyfin>
Devolve um JPEG do quadro em `t`, recortado (x,y,w,h em 0..1) e escalado para
`out` px de largura. O token e validado no proprio Jellyfin (/Users/Me); o caminho
do arquivo vem do item (Fields=Path) e so e servido se estiver sob um --map.
"""
import argparse
import json
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN_TTL_SECONDS = 60
FFMPEG_TIMEOUT_SECONDS = 15
JELLYFIN_TIMEOUT_SECONDS = 5
CACHE_SIZE = 48
MAX_PARALLEL_FFMPEG = 2
# Video sem tag e lido como BT.709 limitado pela TV; sem isto o ffmpeg assume
# BT.601 ao gerar o JPEG e as cores do quadro destoam do video.
SCALE_COLOR = "flags=lanczos:in_color_matrix=bt709:in_range=tv:out_range=pc,format=yuvj420p"
MIN_OUT, MAX_OUT = 320, 3840
CLIP_FPS = 24
CLIP_FRAMES = CLIP_FPS * 4


class BadRequest(ValueError):
    """Parametro invalido; a mensagem diz o valor recebido e o esperado."""


def parse_frame_query(query: dict) -> dict:
    """Valida os parametros de /frame; devolve numeros prontos para o ffmpeg."""
    def number(name: str, default: str) -> float:
        raw = query.get(name, [default])[0]
        try:
            return float(raw)
        except ValueError:
            raise BadRequest(f"{name}={raw!r} invalido, esperado numero") from None

    item = query.get("item", [""])[0]
    if not item.isalnum():
        raise BadRequest(f"item={item!r} invalido, esperado id alfanumerico do Jellyfin")
    t, x, y = number("t", "0"), number("x", "0"), number("y", "0")
    w, h, out = number("w", "1"), number("h", "1"), int(number("out", "1920"))
    if t < 0:
        raise BadRequest(f"t={t} invalido, esperado >= 0 (segundos)")
    for name, value in (("w", w), ("h", h)):
        if not 0.02 <= value <= 1:
            raise BadRequest(f"{name}={value} invalido, esperado entre 0.02 e 1")
    if x < 0 or y < 0 or x + w > 1.0001 or y + h > 1.0001:
        raise BadRequest(f"recorte x={x} y={y} w={w} h={h} fora do quadro (0..1)")
    if not MIN_OUT <= out <= MAX_OUT:
        raise BadRequest(f"out={out} invalido, esperado {MIN_OUT}..{MAX_OUT}")
    return {"item": item, "t": t, "x": x, "y": y, "w": w, "h": h, "out": out}


def map_media_path(container_path: str, mappings: list) -> Path:
    """Traduz o caminho do Jellyfin (no container) para o do disco; bloqueia fuga."""
    for container_root, host_root in mappings:
        root = Path(container_root)
        candidate = Path(container_path)
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        resolved = (Path(host_root) / relative).resolve()
        if Path(host_root).resolve() in resolved.parents:
            return resolved
        raise BadRequest(f"caminho {container_path!r} sai de {host_root!r}")
    raise BadRequest(f"caminho {container_path!r} fora dos --map {[m[0] for m in mappings]}")


def build_ffmpeg_args(media_file: Path, params: dict, extra_filter: str = "") -> list:
    crop = (f"crop=iw*{params['w']}:ih*{params['h']}:"
            f"iw*{params['x']}:ih*{params['y']},scale={params['out']}:-2:{SCALE_COLOR}")
    if extra_filter:
        crop += f",{extra_filter}"
    return ["ffmpeg", "-v", "error", "-ss", f"{params['t']:.3f}", "-i", str(media_file),
            "-frames:v", "1", "-vf", crop, "-q:v", "3", "-f", "image2pipe",
            "-c:v", "mjpeg", "pipe:1"]


HDR_TRANSFERS = ("smpte2084", "arib-std-b67")


def is_hdr_transfer(transfer: str) -> bool:
    return transfer in HDR_TRANSFERS


def probe_color_transfer(media_file: Path) -> str:
    """Le a curva de transferencia do video (ex.: smpte2084 = HDR10)."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=color_transfer", "-of", "default=nw=1:nk=1", str(media_file)],
        capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe falhou em {str(media_file)!r}: {result.stderr.decode()[:200]}")
    return result.stdout.decode().strip()


def build_clip_args(media_file: Path, params: dict, output: Path, hdr: bool = False) -> list:
    """MP4 de um quadro repetido; sem converter cor, a TV le como o filme.

    HDR sai em HEVC 10 bits (hvc1) para manter PQ/HLG; SDR em H.264 8 bits.
    """
    pixel_format = "yuv420p10le" if hdr else "yuv420p"
    crop = (f"crop=iw*{params['w']}:ih*{params['h']}:"
            f"iw*{params['x']}:ih*{params['y']},scale={params['out']}:-2:flags=lanczos,"
            f"format={pixel_format},loop=loop={CLIP_FRAMES}:size=1:start=0,"
            f"setpts=N/{CLIP_FPS}/TB")
    codec = (["-c:v", "libx265", "-preset", "ultrafast", "-crf", "14", "-tag:v", "hvc1"]
             if hdr else ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "12"])
    return ["ffmpeg", "-v", "error", "-y", "-ss", f"{params['t']:.3f}", "-i", str(media_file),
            "-vf", crop, "-frames:v", str(CLIP_FRAMES), "-r", str(CLIP_FPS), "-an",
            *codec, "-movflags", "+faststart", str(output)]


def parse_range(header: str | None, size: int) -> tuple | None:
    """Interpreta 'bytes=a-b' (b opcional); None se nao houver cabecalho."""
    if header is None:
        return None
    match = re.fullmatch(r"bytes=(\d+)-(\d*)", header.strip())
    if not match:
        raise BadRequest(f"Range={header!r} invalido, esperado 'bytes=inicio-fim'")
    start = int(match.group(1))
    end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
    if start > end:
        raise BadRequest(f"Range={header!r} fora do arquivo de {size} bytes")
    return start, end


class JellyfinClient:
    """Valida tokens e descobre o caminho de um item, sempre com o token do chamador."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.valid_until: dict = {}
        self.paths: dict = {}

    def _get(self, path: str, token: str) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}{path}", headers={"Authorization": f'MediaBrowser Token="{token}"'})
        with urllib.request.urlopen(request, timeout=JELLYFIN_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode())

    def check_token(self, token: str) -> None:
        if self.valid_until.get(token, 0) > time.time():
            return
        self._get("/Users/Me", token)  # 401 do Jellyfin vira HTTPError
        self.valid_until[token] = time.time() + TOKEN_TTL_SECONDS

    def item_path(self, item: str, token: str) -> str:
        if item not in self.paths:
            data = self._get(f"/Items?Ids={item}&Fields=Path", token)
            items = data.get("Items") or []
            if not items or not items[0].get("Path"):
                raise BadRequest(f"item={item!r} sem Path no Jellyfin")
            self.paths[item] = items[0]["Path"]
        return self.paths[item]


class FrameCache:
    def __init__(self) -> None:
        self.entries: OrderedDict = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key: tuple):
        with self.lock:
            if key in self.entries:
                self.entries.move_to_end(key)
                return self.entries[key]
        return None

    def put(self, key: tuple, jpeg: bytes) -> None:
        with self.lock:
            self.entries[key] = jpeg
            while len(self.entries) > CACHE_SIZE:
                self.entries.popitem(last=False)


def render_frame(media_file: Path, params: dict, gate: threading.Semaphore,
                 extra_filter: str = "") -> bytes:
    with gate:
        result = subprocess.run(build_ffmpeg_args(media_file, params, extra_filter),
                                capture_output=True,
                                timeout=FFMPEG_TIMEOUT_SECONDS, check=False)
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError(f"ffmpeg falhou ({result.returncode}): {result.stderr.decode()[:200]}")
    return result.stdout


def render_clip(media_file: Path, params: dict, gate: threading.Semaphore) -> bytes:
    hdr = is_hdr_transfer(probe_color_transfer(media_file))
    with gate, tempfile.TemporaryDirectory() as folder:
        output = Path(folder) / "clip.mp4"
        result = subprocess.run(build_clip_args(media_file, params, output, hdr),
                                capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS * 2,
                                check=False)
        if result.returncode != 0 or not output.exists():
            raise RuntimeError(f"ffmpeg falhou ({result.returncode}): {result.stderr.decode()[:200]}")
        return output.read_bytes()


def make_handler(jellyfin: JellyfinClient, mappings: list, extra_filter: str = "") -> type:
    cache, gate = FrameCache(), threading.Semaphore(MAX_PARALLEL_FFMPEG)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            print(f"[frame_server] {self.address_string()} {fmt % args}", flush=True)

        def _reply(self, status: int, body: bytes, content_type: str = "text/plain") -> None:
            try:
                span = parse_range(self.headers.get("Range"), len(body)) if status == 200 else None
            except BadRequest as error:
                self._reply(416, str(error).encode())
                return
            if span:
                status, body = 206, body[span[0]:span[1] + 1]
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _produce(self, kind: str, query: dict) -> tuple:
            params = parse_frame_query(query)
            token = query.get("api_key", [""])[0]
            jellyfin.check_token(token)
            media_file = map_media_path(jellyfin.item_path(params["item"], token), mappings)
            key = (kind, extra_filter, str(media_file), round(params["t"], 1), params["x"],
                   params["y"], params["w"], params["h"], params["out"])
            body = cache.get(key)
            if body is None and kind == "clip":
                body = render_clip(media_file, params, gate)
            elif body is None:
                body = render_frame(media_file, params, gate, extra_filter)
            cache.put(key, body)
            return body, ("video/mp4" if kind == "clip" else "image/jpeg")

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            kind = parsed.path.lstrip("/")
            if kind not in ("frame", "clip"):
                self._reply(404, b"use /frame ou /clip")
                return
            try:
                body, content_type = self._produce(kind, urllib.parse.parse_qs(parsed.query))
            except BadRequest as error:
                self._reply(400, str(error).encode())
            except urllib.error.HTTPError as error:
                self._reply(401 if error.code == 401 else 502, f"jellyfin: {error}".encode())
            except (urllib.error.URLError, RuntimeError, subprocess.TimeoutExpired) as error:
                self._reply(502, f"falha: {error}".encode())
            else:
                self._reply(200, body, content_type)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jellyfin", required=True, help="URL do Jellyfin, ex.: http://127.0.0.1:8096")
    parser.add_argument("--map", action="append", required=True, metavar="CONTAINER=DISCO",
                        help="mapeia o caminho do Jellyfin para o disco (repetivel)")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--eq", default="", help="filtro ffmpeg extra para casar com a TV, ex.: eq=contrast=1.1:gamma=0.95")
    args = parser.parse_args()
    mappings = []
    for raw in args.map:
        container_root, _, host_root = raw.partition("=")
        if not container_root or not host_root:
            raise SystemExit(f"--map {raw!r} invalido, esperado CONTAINER=DISCO")
        mappings.append((container_root, host_root))
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(JellyfinClient(args.jellyfin), mappings, args.eq))
    print(f"[frame_server] http://{args.bind}:{args.port}/frame  (Jellyfin {args.jellyfin}, maps {mappings})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
